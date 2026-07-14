import pytest

from harness.base import PoliteSession
from harness.transport import HttpResponse, SafeHttpTransport, TransportError
from harness.url_safety import UnsafeUrlError


def public_resolver(host):
    return ["93.184.216.34"] if host != "internal.example" else ["10.0.0.1"]


def test_redirect_target_is_revalidated_before_second_request():
    calls = []

    def request_once(url, hostname, port, address, headers, timeout, max_bytes):
        calls.append(url)
        return HttpResponse(url, 302, {"location": "http://internal.example/secret"}, b"")

    transport = SafeHttpTransport(resolver=public_resolver, request_once=request_once)
    with pytest.raises(UnsafeUrlError, match="non-public"):
        transport.get("https://example.com/start")
    assert calls == ["https://example.com/start"]


def test_cross_origin_redirect_strips_credentials():
    seen = []

    def request_once(url, hostname, port, address, headers, timeout, max_bytes):
        seen.append(dict(headers))
        if len(seen) == 1:
            return HttpResponse(url, 302, {"Location": "https://other.example/end"}, b"")
        return HttpResponse(url, 200, {"content-type": "text/plain"}, b"ok")

    transport = SafeHttpTransport(resolver=public_resolver, request_once=request_once)
    response = transport.get(
        "https://example.com/start", headers={"Authorization": "Bearer secret", "X-Test": "yes"}
    )
    assert response.text == "ok"
    assert seen[0]["Authorization"] == "Bearer secret"
    assert "Authorization" not in seen[1]
    assert seen[1]["X-Test"] == "yes"


def test_redirect_limit_and_argument_validation():
    def redirect(url, hostname, port, address, headers, timeout, max_bytes):
        return HttpResponse(url, 302, {"location": "/again"}, b"")

    transport = SafeHttpTransport(resolver=public_resolver, request_once=redirect)
    with pytest.raises(TransportError, match="exceeded"):
        transport.get("https://example.com/start", max_redirects=1)
    with pytest.raises(ValueError):
        transport.get("https://example.com", max_bytes=0)


def test_callers_cannot_override_transport_safety_headers():
    seen = {}

    def request_once(url, hostname, port, address, headers, timeout, max_bytes):
        seen.update(headers)
        return HttpResponse(url, 200, {}, b"ok")

    transport = SafeHttpTransport(resolver=public_resolver, request_once=request_once)
    transport.get(
        "https://example.com",
        headers={"host": "internal.example", "Accept-Encoding": "gzip", "X-Test": "yes"},
    )
    assert seen == {"X-Test": "yes"}


def test_robots_uses_protected_transport():
    calls = 0

    def request_once(url, hostname, port, address, headers, timeout, max_bytes):
        nonlocal calls
        calls += 1
        assert url == "https://example.com/robots.txt"
        return HttpResponse(
            url, 200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /private"
        )

    transport = SafeHttpTransport(resolver=public_resolver, request_once=request_once)
    assert not transport.robots_allows("https://example.com/private/page")
    assert transport.robots_allows("https://example.com/public")
    assert calls == 1


def test_polite_session_delegates_get_to_safe_transport():
    class FakeTransport:
        def get(self, url, **kwargs):
            assert kwargs["max_bytes"] == 123
            return HttpResponse(url, 200, {"Content-Type": "application/json"}, b'{"ok": true}')

    session = PoliteSession(min_interval=0)
    session.transport = FakeTransport()
    assert session.get_json("https://example.com", max_bytes=123) == {"ok": True}
