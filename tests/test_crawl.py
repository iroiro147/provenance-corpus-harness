import json
from urllib.parse import urlsplit

import pytest

from harness.browser import BrowserRequest, RenderedPage
from harness.crawl import (
    CrawlFrontier,
    CrawlPolicy,
    FrontierSkipReason,
    FrontierStatus,
    collect_site,
    verify_site_receipt,
    write_site_receipt,
)
from harness.transport import HttpResponse, ResponseTooLargeError


def test_frontier_is_breadth_first_canonical_and_deduplicated():
    frontier = CrawlFrontier(
        "https://example.com/start?utm_source=x",
        CrawlPolicy(max_depth=2, respect_robots=False),
    )
    seed = frontier.pop()
    assert seed is not None
    assert seed.canonical_url == "https://example.com/start"
    frontier.discover(
        ["/a?b=2&a=1#fragment", "/b", "/a?a=1&b=2"],
        parent_url=seed.canonical_url,
        depth=1,
    )
    frontier.mark_done(seed.canonical_url)

    assert frontier.pop().canonical_url == "https://example.com/a?a=1&b=2"
    assert frontier.pop().canonical_url == "https://example.com/b"
    assert len([entry for entry in frontier.entries if "/a?" in entry.canonical_url]) == 1


def test_cross_origin_is_skipped_unless_origin_is_explicitly_allowed():
    closed = CrawlFrontier("https://example.com", CrawlPolicy(respect_robots=False))
    closed.pop()
    skipped = closed.discover(
        ["https://cdn.example.net/app.js"],
        parent_url="https://example.com/",
        depth=1,
    )[0]
    assert skipped.status is FrontierStatus.SKIPPED
    assert skipped.skip_reason is FrontierSkipReason.CROSS_ORIGIN

    open_to_one = CrawlFrontier(
        "https://example.com",
        CrawlPolicy(
            same_origin=False,
            allowed_origins=("https://cdn.example.net",),
            respect_robots=False,
        ),
    )
    open_to_one.pop()
    queued = open_to_one.discover(
        ["https://cdn.example.net/app.js", "https://other.example/no"],
        parent_url="https://example.com/",
        depth=1,
    )
    assert queued[0].status is FrontierStatus.QUEUED
    assert queued[1].skip_reason is FrontierSkipReason.CROSS_ORIGIN


def test_robots_denial_and_failure_are_explicit_fail_closed_states():
    def robots(url):
        if url.endswith("/error"):
            raise RuntimeError("robots unavailable")
        return not url.endswith("/private")

    frontier = CrawlFrontier("https://example.com", robots_checker=robots)
    frontier.pop()
    results = frontier.discover(
        ["/private", "/error", "/public"],
        parent_url="https://example.com/",
        depth=1,
    )
    assert [entry.skip_reason for entry in results] == [
        FrontierSkipReason.ROBOTS_DISALLOWED,
        FrontierSkipReason.ROBOTS_UNAVAILABLE,
        None,
    ]


def test_deny_wins_allow_and_depth_is_bounded():
    frontier = CrawlFrontier(
        "https://example.com/docs/",
        CrawlPolicy(
            max_depth=1,
            allow_patterns=("https://example.com/docs/*",),
            deny_patterns=("*/private*",),
            respect_robots=False,
        ),
    )
    frontier.pop()
    results = frontier.discover(
        ["/docs/ok", "/docs/private", "/other", "/docs/deep"],
        parent_url="https://example.com/docs/",
        depth=2,
    )
    assert all(entry.skip_reason is FrontierSkipReason.MAX_DEPTH for entry in results)

    shallow = CrawlFrontier(frontier.seed, frontier.policy)
    shallow.pop()
    results = shallow.discover(
        ["/docs/ok", "/docs/private", "/other"],
        parent_url=shallow.seed,
        depth=1,
    )
    assert results[0].status is FrontierStatus.QUEUED
    assert results[1].skip_reason is FrontierSkipReason.POLICY_DENIED
    assert results[2].skip_reason is FrontierSkipReason.POLICY_DENIED


def test_page_and_frontier_budgets_stop_work_without_losing_events():
    frontier = CrawlFrontier(
        "https://example.com",
        CrawlPolicy(
            max_pages=2,
            max_frontier_entries=3,
            max_links_per_page=4,
            respect_robots=False,
        ),
    )
    seed = frontier.pop()
    discovered = frontier.discover(
        ["/a", "/b", "/c", "/d", "/ignored"],
        parent_url=seed.canonical_url,
        depth=1,
    )
    frontier.mark_done(seed.canonical_url)

    assert len(discovered) == 4
    assert discovered[2].skip_reason is FrontierSkipReason.FRONTIER_LIMIT
    assert discovered[3].skip_reason is FrontierSkipReason.FRONTIER_LIMIT
    assert len(frontier.entries) == 3
    assert frontier.pop().canonical_url.endswith("/a")
    assert frontier.pop() is None
    assert not frontier.exhausted
    assert frontier.page_limit_reached
    assert [event.status for event in frontier.events[:2]] == [
        FrontierStatus.QUEUED,
        FrontierStatus.IN_PROGRESS,
    ]


def test_unsafe_urls_are_never_persisted_in_entries_or_events():
    frontier = CrawlFrontier("https://example.com", CrawlPolicy(respect_robots=False))
    frontier.pop()
    result = frontier.discover(
        [
            "http://127.0.0.1/private",
            "https://user:pass@example.com/private",
            "/ok?api_key=secret",
        ],
        parent_url="https://example.com/",
        depth=1,
    )
    assert result == ()
    rendered = repr(frontier.events)
    assert "pass" not in rendered and "secret" not in rendered


def test_invalid_cross_origin_policy_is_rejected():
    with pytest.raises(ValueError, match="explicit allowed_origins"):
        CrawlPolicy(same_origin=False)


def test_failure_error_is_redacted_and_bounded():
    frontier = CrawlFrontier("https://example.com", CrawlPolicy(respect_robots=False))
    seed = frontier.pop()
    failed = frontier.mark_failed(
        seed.canonical_url,
        "failed at https://example.com/x?token=secret " + "x" * 2_000,
    )
    assert "secret" not in failed.error
    assert "token" not in failed.error
    assert len(failed.error) == 1_000


class FakeSiteTransport:
    def __init__(self, pages, *, disallowed=(), robots_error=False):
        self.pages = pages
        self.disallowed = set(disallowed)
        self.robots_error = robots_error
        self.fetches = []
        self.robots_checks = []

    def get(self, url, *, timeout, max_bytes, max_redirects, headers=None):
        if url.endswith("/robots.txt"):
            self.robots_checks.append(url)
            if self.robots_error:
                raise RuntimeError("robots unavailable")
            requested = urlsplit(url)
            rules = []
            for denied in sorted(self.disallowed):
                parsed = urlsplit(denied)
                if (parsed.scheme, parsed.netloc) == (requested.scheme, requested.netloc):
                    rules.append(f"Disallow: {parsed.path or '/'}")
            body = "User-agent: *\n" + "\n".join(rules)
            raw = body.encode()
            if len(raw) > max_bytes:
                raise ResponseTooLargeError("fixture exceeded robots budget")
            return HttpResponse(url, 200, {"content-type": "text/plain"}, raw)
        self.fetches.append(url)
        status, content_type, body = self.pages[url]
        raw = body.encode()
        if len(raw) > max_bytes:
            raise ResponseTooLargeError("fixture exceeded page budget")
        return HttpResponse(url, status, {"content-type": content_type}, raw)


def test_collect_site_fetches_extracts_writes_and_crawls_breadth_first(tmp_path):
    pages = {
        "https://example.com/": (
            200,
            "text/html; charset=utf-8",
            """<html><head><title>Home</title><link rel="canonical" href="/"/></head>
            <body><main><p>Home copy.</p><a href="/a?utm_source=x">A</a>
            <a href="/b">B</a><a href="https://other.example/no">No</a></main></body></html>""",
        ),
        "https://example.com/a": (
            200,
            "text/html",
            "<html><head><title>A</title></head><body><p>Article A.</p></body></html>",
        ),
        "https://example.com/b": (
            200,
            "text/html",
            "<html><head><title>B</title></head><body><p>Article B.</p></body></html>",
        ),
    }
    transport = FakeSiteTransport(pages)

    result = collect_site(
        "https://example.com/",
        tmp_path,
        policy=CrawlPolicy(max_pages=3, max_depth=1, min_interval=0),
        transport=transport,
        scraped_at="2026-07-14T00:00:00+00:00",
    )

    assert result.status == "complete"
    assert result.written == 3 and result.failed == 0
    assert transport.fetches == [
        "https://example.com/",
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert [page.title for page in result.pages] == ["Home", "A", "B"]
    assert all(path.exists() for path in result.paths)
    assert "Home copy" in result.paths[0].read_text()
    skipped = [
        event
        for event in result.frontier_events
        if event.skip_reason is FrontierSkipReason.CROSS_ORIGIN
    ]
    assert len(skipped) == 1
    assert transport.robots_checks == ["https://example.com/robots.txt"]
    assert result.total_bytes == sum(len(body.encode()) for _, _, body in pages.values()) + len(
        b"User-agent: *\n"
    )

    receipt = write_site_receipt(result, tmp_path, created_at="2026-07-14T00:00:00+00:00")
    payload = json.loads(receipt.read_text())
    assert payload["contract"] == "provenance-site-acquisition.v1"
    assert payload["paths"] == [path.relative_to(tmp_path).as_posix() for path in result.paths]
    assert str(tmp_path) not in receipt.read_text()
    assert payload["policy"]["max_pages"] == 3
    assert all(page["record_hash"] for page in payload["pages"])
    assert verify_site_receipt(receipt, tmp_path).ok


def test_collect_site_enforces_content_type_and_page_byte_budget(tmp_path):
    bad_type = FakeSiteTransport(
        {"https://example.com/": (200, "application/octet-stream", "bytes")}
    )
    result = collect_site(
        "https://example.com/",
        tmp_path / "type",
        policy=CrawlPolicy(min_interval=0),
        transport=bad_type,
    )
    assert result.status == "failed" and result.failed == 1
    assert "content type" in result.pages[0].error

    oversized = FakeSiteTransport({"https://example.com/": (200, "text/html", "x" * 20)})
    result = collect_site(
        "https://example.com/",
        tmp_path / "size",
        policy=CrawlPolicy(
            max_page_bytes=10,
            max_total_bytes=10,
            min_interval=0,
            respect_robots=False,
        ),
        transport=oversized,
    )
    assert result.status == "failed" and result.total_bytes == 0
    assert "page budget" in result.pages[0].error


def test_collect_site_obeys_politeness_delay(tmp_path):
    pages = {
        "https://example.com/": (200, "text/html", '<a href="/a">A</a>'),
        "https://example.com/a": (200, "text/html", "Article"),
    }
    ticks = iter([0.0, 0.25, 1.0])
    sleeps = []
    result = collect_site(
        "https://example.com/",
        tmp_path,
        policy=CrawlPolicy(max_pages=2, min_interval=1.0, respect_robots=False),
        transport=FakeSiteTransport(pages),
        clock=lambda: next(ticks),
        sleep=sleeps.append,
    )
    assert result.written == 2
    assert sleeps == [0.75]


def test_collect_site_can_use_injected_no_egress_renderer(tmp_path):
    class FakeRenderer:
        primitive = "fixture-browser"

        def render(self, url, *, gateway, policy):
            response = gateway.fetch(BrowserRequest(url))
            return RenderedPage(
                requested_url=url,
                final_url=response.final_url,
                title="Rendered title",
                html="<html><head><title>Rendered title</title></head><body><p>JS copy.</p></body></html>",
                status_code=200,
                content_type="text/html",
                screenshot=None,
                request_count=gateway.request_count,
                network_bytes=gateway.network_bytes,
                source_primitive=self.primitive,
            )

    transport = FakeSiteTransport(
        {"https://example.com/": (200, "text/html", "<div id='app'></div>")}
    )
    result = collect_site(
        "https://example.com/",
        tmp_path,
        policy=CrawlPolicy(min_interval=0),
        transport=transport,
        renderer=FakeRenderer(),
    )
    assert result.status == "complete" and result.written == 1
    assert result.pages[0].source_profile == "browser"
    assert result.pages[0].title == "Rendered title"
    assert "JS copy" in result.paths[0].read_text()


def test_collect_site_debits_browser_bytes_when_render_fails(tmp_path):
    class FailingRenderer:
        primitive = "fixture-failing-browser"

        def render(self, url, *, gateway, policy):
            gateway.fetch(BrowserRequest(url))
            raise RuntimeError("render process failed")

    body = "<div>partially acquired</div>"
    result = collect_site(
        "https://example.com/",
        tmp_path,
        policy=CrawlPolicy(min_interval=0, respect_robots=False),
        transport=FakeSiteTransport({"https://example.com/": (200, "text/html", body)}),
        renderer=FailingRenderer(),
    )
    assert result.status == "failed" and result.failed == 1
    assert result.fetched_pages == 0
    assert result.total_bytes == len(body.encode())
    assert "BrowserRenderFailure" in result.pages[0].error


def test_collect_site_robots_denial_never_fetches_seed(tmp_path):
    transport = FakeSiteTransport(
        {"https://example.com/": (200, "text/html", "must not fetch")},
        disallowed={"https://example.com/"},
    )
    result = collect_site("https://example.com/", tmp_path, transport=transport)
    assert result.status == "complete"
    assert result.failed == 0
    assert result.fetched_pages == 0 and not result.paths
    assert transport.fetches == []
    assert result.frontier_events[0].skip_reason is FrontierSkipReason.ROBOTS_DISALLOWED


def test_collect_site_robots_unavailable_fails_closed_and_fails_the_run(tmp_path):
    transport = FakeSiteTransport(
        {"https://example.com/": (200, "text/html", "must not fetch")},
        robots_error=True,
    )
    result = collect_site("https://example.com/", tmp_path, transport=transport)
    assert result.status == "failed"
    assert result.failed == 1 and result.fetched_pages == 0
    assert transport.fetches == []
    assert result.pages[0].url == "https://example.com/"
    assert result.pages[0].outcome == "failed"
    assert "failed closed" in result.pages[0].error
    assert result.frontier_events[0].skip_reason is FrontierSkipReason.ROBOTS_UNAVAILABLE


def test_collect_site_reports_page_limit_without_claiming_frontier_exhaustion(tmp_path):
    transport = FakeSiteTransport(
        {
            "https://example.com/": (200, "text/html", '<a href="/later">Later</a>'),
            "https://example.com/later": (200, "text/html", "Later"),
        }
    )
    result = collect_site(
        "https://example.com/",
        tmp_path,
        policy=CrawlPolicy(max_pages=1, max_depth=1, min_interval=0),
        transport=transport,
    )
    assert result.written == 1
    assert result.page_limit_reached
    assert not result.frontier_exhausted
    assert transport.fetches == ["https://example.com/"]


def test_site_receipt_confines_timestamp_and_verifies_record_hashes(tmp_path):
    pages = {"https://example.com/": (200, "text/markdown", "# Stable\n\nBody")}
    result = collect_site(
        "https://example.com/",
        tmp_path,
        policy=CrawlPolicy(min_interval=0),
        transport=FakeSiteTransport(pages),
    )

    with pytest.raises(ValueError, match="ISO-8601"):
        write_site_receipt(result, tmp_path, created_at="../../../escaped")
    with pytest.raises(ValueError, match="timezone"):
        write_site_receipt(result, tmp_path, created_at="2026-07-14T00:00:00")
    assert not (tmp_path.parent / "escaped").exists()

    receipt = write_site_receipt(result, tmp_path, created_at="2026-07-14T00:00:00Z")
    verification = verify_site_receipt(receipt, tmp_path)
    assert verification.ok and verification.checked == 1

    result.paths[0].write_text("tampered", encoding="utf-8")
    verification = verify_site_receipt(receipt, tmp_path)
    assert not verification.ok
    assert verification.checked == 1
    assert verification.errors == ("page 0: record hash mismatch",)


def test_site_receipt_verifier_rejects_semantically_fabricated_receipt(tmp_path):
    result = collect_site(
        "https://example.com/",
        tmp_path,
        policy=CrawlPolicy(min_interval=0),
        transport=FakeSiteTransport(
            {"https://example.com/": (200, "text/markdown", "# Stable\n\nBody")}
        ),
    )
    receipt = write_site_receipt(result, tmp_path, created_at="2026-07-14T00:00:00Z")
    payload = json.loads(receipt.read_text())
    payload["status"] = "complete"
    payload["counts"]["written"] = 999
    payload["paths"] = ["missing.md"]
    payload["pages"] = []
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    verification = verify_site_receipt(receipt, tmp_path)
    assert not verification.ok and verification.checked == 0
    assert "site receipt paths do not match page record paths" in verification.errors
    assert "site receipt written count does not match pages" in verification.errors


def test_site_receipt_verifier_requires_robots_unavailable_failure_outcome(tmp_path):
    result = collect_site(
        "https://example.com/",
        tmp_path,
        transport=FakeSiteTransport(
            {"https://example.com/": (200, "text/html", "unused")},
            robots_error=True,
        ),
    )
    receipt = write_site_receipt(result, tmp_path, created_at="2026-07-14T00:00:00Z")
    assert verify_site_receipt(receipt, tmp_path).ok

    payload = json.loads(receipt.read_text())
    payload["pages"] = []
    payload["counts"]["failed"] = 0
    payload["status"] = "complete"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    verification = verify_site_receipt(receipt, tmp_path)
    assert not verification.ok
    assert "robots-unavailable events require failed page outcomes" in verification.errors


def test_collect_site_reports_duplicates_exactly(tmp_path):
    pages = {"https://example.com/": (200, "text/markdown", "# Stable\n\nBody")}
    policy = CrawlPolicy(min_interval=0)
    first = collect_site(
        "https://example.com/", tmp_path, policy=policy, transport=FakeSiteTransport(pages)
    )
    second = collect_site(
        "https://example.com/", tmp_path, policy=policy, transport=FakeSiteTransport(pages)
    )
    assert first.written == 1 and first.duplicates == 0
    assert second.written == 0 and second.duplicates == 1
    assert second.pages[0].outcome == "duplicate"
    assert second.paths == first.paths
