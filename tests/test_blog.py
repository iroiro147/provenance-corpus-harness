import pytest

from harness.scrapers.blog import BlogScraper
from harness.transport import HttpResponse, SafeHttpTransport
from harness.url_safety import UnsafeUrlError

HTML = """<!DOCTYPE html><html><head>
<title>My Article Title</title>
<meta property="article:author" content="Jane Doe">
</head><body>
<nav>Home About Contact</nav>
<article>
<h1>My Article Title</h1>
<p>This is the first substantial paragraph of the article body, long enough that
trafilatura recognizes it as the main content rather than boilerplate navigation.</p>
<p>A second paragraph continues the thought with more real sentences so the extractor
keeps it as article text and not as a menu or a sidebar widget.</p>
</article>
<footer>Copyright 2026</footer></body></html>"""


def test_blog_extract_offline():
    scraper = BlogScraper(respect_robots=False)
    item = scraper.extract("https://example.com/my-article", downloaded=HTML)

    assert item is not None
    assert item.source_url == "https://example.com/my-article"
    assert "first substantial paragraph" in item.body
    assert "second paragraph continues" in item.body
    assert "Home About Contact" not in item.body  # nav stripped by trafilatura
    assert item.title  # metadata title (best-effort), or URL fallback — never empty


def test_blog_extract_empty_returns_none():
    scraper = BlogScraper(respect_robots=False)
    assert scraper.extract("https://example.com/x", downloaded="<html><body></body></html>") is None
    assert scraper.extract("https://example.com/x", downloaded="") is None


def test_blog_default_path_uses_protected_transport():
    class FakeTransport:
        def robots_allows(self, url, **kwargs):
            return True

        def get(self, url, **kwargs):
            return HttpResponse(
                url, 200, {"Content-Type": "text/html; charset=utf-8"}, HTML.encode()
            )

    scraper = BlogScraper(transport=FakeTransport())
    scraper.http.min_interval = 0
    items = list(scraper.scrape("https://example.com/article"))
    assert len(items) == 1


def test_blog_rejects_private_redirect_before_content_fetch():
    def resolver(host):
        return ["10.0.0.1"] if host == "internal.example" else ["93.184.216.34"]

    def request_once(url, hostname, port, address, headers, timeout, max_bytes):
        return HttpResponse(url, 302, {"Location": "http://internal.example/secret"}, b"")

    transport = SafeHttpTransport(resolver=resolver, request_once=request_once)
    scraper = BlogScraper(transport=transport, respect_robots=False)
    scraper.http.min_interval = 0
    with pytest.raises(UnsafeUrlError):
        list(scraper.scrape("https://example.com/article"))
