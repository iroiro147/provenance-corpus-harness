import json

import pytest

from harness.browser import (
    BrowserBudgetExceeded,
    BrowserNetworkGateway,
    BrowserPolicy,
    BrowserRenderFailure,
    BrowserRequest,
    BrowserRequestDenied,
    RenderedPage,
    render_page,
    verify_browser_receipt,
    write_browser_receipt,
)
from harness.transport import HttpResponse, SafeHttpTransport, TransportError


def public_resolver(_host):
    return ["93.184.216.34"]


def transport_for(responder):
    return SafeHttpTransport(resolver=public_resolver, request_once=responder)


def test_gateway_fulfills_get_through_safe_transport_and_filters_headers():
    seen = {}

    def request_once(url, hostname, port, address, headers, timeout, max_bytes):
        seen.update(headers)
        return HttpResponse(
            url,
            200,
            {
                "content-type": "text/javascript",
                "set-cookie": "session=secret",
                "x-internal": "no",
            },
            b"console.log('ok')",
        )

    gateway = BrowserNetworkGateway(
        "https://example.com",
        transport=transport_for(request_once),
    )
    response = gateway.fetch(
        BrowserRequest(
            "https://example.com/app.js",
            headers={
                "Accept": "text/javascript",
                "Authorization": "Bearer secret",
                "Cookie": "session=secret",
            },
            resource_type="script",
        )
    )

    assert seen == {"Accept": "text/javascript"}
    assert response.body == b"console.log('ok')"
    assert response.headers == {
        "content-type": "text/javascript",
        "content-length": "17",
    }
    assert gateway.request_count == 1 and gateway.network_bytes == 17


def test_gateway_denies_state_changes_and_unlisted_origins_before_transport():
    calls = 0

    def request_once(*args):
        nonlocal calls
        calls += 1
        return HttpResponse(args[0], 200, {}, b"ok")

    gateway = BrowserNetworkGateway("https://example.com", transport=transport_for(request_once))
    with pytest.raises(BrowserRequestDenied, match="only GET"):
        gateway.fetch(BrowserRequest("https://example.com/form", method="POST"))
    with pytest.raises(BrowserRequestDenied, match="origin"):
        gateway.fetch(BrowserRequest("https://other.example/script.js"))
    assert calls == 0


def test_explicit_origin_and_network_budgets_are_enforced():
    def request_once(url, hostname, port, address, headers, timeout, max_bytes):
        return HttpResponse(url, 200, {"content-type": "text/plain"}, b"1234")

    gateway = BrowserNetworkGateway(
        "https://example.com",
        policy=BrowserPolicy(
            allowed_origins=("https://cdn.example.net",),
            max_requests=2,
            max_resource_bytes=4,
            max_total_bytes=8,
        ),
        transport=transport_for(request_once),
    )
    gateway.fetch(BrowserRequest("https://example.com/"))
    gateway.fetch(BrowserRequest("https://cdn.example.net/a.js"))
    with pytest.raises(BrowserBudgetExceeded, match="request budget"):
        gateway.fetch(BrowserRequest("https://example.com/third"))


def test_redirects_are_fail_closed_until_each_hop_can_check_origin():
    def request_once(url, hostname, port, address, headers, timeout, max_bytes):
        return HttpResponse(url, 302, {"location": "https://other.example/end"}, b"")

    gateway = BrowserNetworkGateway("https://example.com", transport=transport_for(request_once))
    with pytest.raises(TransportError, match="exceeded 0 redirects"):
        gateway.fetch(BrowserRequest("https://example.com/start"))
    with pytest.raises(ValueError, match="every-hop"):
        BrowserPolicy(max_redirects=1)


class FakeDriver:
    primitive = "fake-browser"

    def __init__(self):
        self.gateway = None

    def render(self, url, *, gateway, policy):
        self.gateway = gateway
        document = gateway.fetch(BrowserRequest(url))
        return RenderedPage(
            requested_url=url,
            final_url=document.final_url,
            title="Rendered",
            html="<h1>Rendered</h1>",
            status_code=document.status_code,
            content_type=document.headers["content-type"],
            screenshot=b"png" if policy.capture_screenshot else None,
            request_count=gateway.request_count,
            network_bytes=gateway.network_bytes,
            source_primitive=self.primitive,
        )


def test_render_page_uses_injected_driver_and_reports_gateway_totals():
    def request_once(url, hostname, port, address, headers, timeout, max_bytes):
        return HttpResponse(url, 200, {"content-type": "text/html"}, b"<p>source</p>")

    driver = FakeDriver()
    result = render_page(
        "https://example.com",
        driver=driver,
        policy=BrowserPolicy(capture_screenshot=True),
        transport=transport_for(request_once),
    )
    assert result.title == "Rendered"
    assert result.screenshot == b"png"
    assert result.request_count == 1
    assert result.network_bytes == len(b"<p>source</p>")
    assert result.source_primitive == "fake-browser"
    assert driver.gateway is not None


def test_rendered_page_cannot_claim_an_unlisted_final_origin():
    class EscapingDriver(FakeDriver):
        def render(self, url, *, gateway, policy):
            return RenderedPage(
                requested_url=url,
                final_url="https://other.example/escaped",
                title="No",
                html="",
                status_code=200,
                content_type="text/html",
                screenshot=None,
                request_count=0,
                network_bytes=0,
            )

    with pytest.raises(BrowserRequestDenied, match="escaped"):
        render_page("https://example.com", driver=EscapingDriver())


def test_rendered_page_cannot_claim_a_non_http_final_url():
    class DataUrlDriver(FakeDriver):
        def render(self, url, *, gateway, policy):
            return RenderedPage(
                requested_url=url,
                final_url="data:text/html,escaped",
                title="No",
                html="",
                status_code=200,
                content_type="text/html",
                screenshot=None,
                request_count=0,
                network_bytes=0,
            )

    with pytest.raises(BrowserRequestDenied, match="safe public URL"):
        render_page("https://example.com", driver=DataUrlDriver())


def test_render_failure_preserves_consumed_gateway_totals():
    def request_once(url, hostname, port, address, headers, timeout, max_bytes):
        return HttpResponse(url, 200, {"content-type": "text/html"}, b"partial")

    class FailingDriver:
        primitive = "failing-browser"

        def render(self, url, *, gateway, policy):
            gateway.fetch(BrowserRequest(url))
            raise RuntimeError("renderer crashed")

    with pytest.raises(BrowserRenderFailure) as failure:
        render_page(
            "https://example.com",
            driver=FailingDriver(),
            transport=transport_for(request_once),
        )
    assert failure.value.request_count == 1
    assert failure.value.network_bytes == len(b"partial")
    assert isinstance(failure.value.__cause__, RuntimeError)


def test_browser_receipt_is_relative_sanitized_and_verifiable(tmp_path):
    record = tmp_path / "browser" / "rendered.md"
    record.parent.mkdir()
    record.write_text("# Rendered\n\nEvidence", encoding="utf-8")
    asset_id = "asset-" + "a" * 64
    (tmp_path / "assets.json").write_text(
        json.dumps(
            {
                "contract": "provenance-assets.v1",
                "assets": [{"asset_id": asset_id}],
            }
        ),
        encoding="utf-8",
    )
    page = RenderedPage(
        requested_url="https://example.com/app?token=secret",
        final_url="https://example.com/app?utm_source=test",
        title="Rendered",
        html="<h1>Rendered</h1>",
        status_code=200,
        content_type="text/html",
        screenshot=None,
        request_count=3,
        network_bytes=123,
        source_primitive="fixture-browser",
    )
    receipt = write_browser_receipt(
        page,
        record,
        tmp_path,
        outcome="written",
        asset_ids=(asset_id,),
        created_at="2026-07-14T00:00:00Z",
    )
    payload = json.loads(receipt.read_text())
    assert payload["record_path"] == "browser/rendered.md"
    assert payload["requested_url"] == "https://example.com/app"
    assert payload["final_url"] == "https://example.com/app?utm_source=test"
    assert "secret" not in receipt.read_text()
    verification = verify_browser_receipt(receipt, tmp_path)
    assert verification.ok and verification.checked == 2

    record.write_text("tampered", encoding="utf-8")
    verification = verify_browser_receipt(receipt, tmp_path)
    assert not verification.ok
    assert "browser record hash mismatch" in verification.errors


def test_browser_receipt_rejects_traversal_and_semantic_rewrites(tmp_path):
    record = tmp_path / "browser.md"
    record.write_text("record", encoding="utf-8")
    page = RenderedPage(
        requested_url="https://example.com/",
        final_url="https://example.com/",
        title="Rendered",
        html="Rendered",
        status_code=200,
        content_type="text/html",
        screenshot=None,
        request_count=1,
        network_bytes=8,
    )
    with pytest.raises(ValueError, match="ISO-8601"):
        write_browser_receipt(
            page,
            record,
            tmp_path,
            outcome="written",
            created_at="../../../escaped",
        )
    outside = tmp_path.parent / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        write_browser_receipt(page, outside, tmp_path, outcome="written")

    receipt = write_browser_receipt(
        page,
        record,
        tmp_path,
        outcome="written",
        created_at="2026-07-14T00:00:00Z",
    )
    payload = json.loads(receipt.read_text())
    payload["outcome"] = "empty"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    verification = verify_browser_receipt(receipt, tmp_path)
    assert not verification.ok
    assert "browser receipt record path requires a written or duplicate outcome" in (
        verification.errors
    )


def test_browser_receipt_verifier_rejects_unknown_asset_ids(tmp_path):
    record = tmp_path / "browser.md"
    record.write_text("record", encoding="utf-8")
    page = RenderedPage(
        requested_url="https://example.com/",
        final_url="https://example.com/",
        title="Rendered",
        html="Rendered",
        status_code=200,
        content_type="text/html",
        screenshot=None,
        request_count=1,
        network_bytes=8,
    )
    asset_id = "asset-" + "b" * 64
    receipt = write_browser_receipt(
        page,
        record,
        tmp_path,
        outcome="written",
        asset_ids=(asset_id,),
        created_at="2026-07-14T00:00:00Z",
    )
    verification = verify_browser_receipt(receipt, tmp_path)
    assert not verification.ok
    assert "browser asset_ids require a regular assets.json manifest" in verification.errors
