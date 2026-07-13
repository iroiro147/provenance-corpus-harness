import feedparser

from harness.scrapers.rss import RSSScraper

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Test Newsletter</title>
  <item>
    <title>First Post</title>
    <link>https://example.com/first</link>
    <author>writer@example.com</author>
    <pubDate>Sat, 06 Jun 2026 10:00:00 GMT</pubDate>
    <description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt; from the feed.&lt;/p&gt;</description>
  </item>
  <item>
    <title>Second Post</title>
    <link>https://example.com/second</link>
    <description>Plain summary text here.</description>
  </item>
</channel></rss>
"""


def test_rss_parses_entries_offline():
    # feedparser.parse accepts a raw string → no network.
    scraper = RSSScraper(parse=feedparser.parse, platform_name="substack")
    items = list(scraper.scrape(RSS, limit=10))

    assert len(items) == 2
    assert items[0].platform == "substack"
    assert items[0].title == "First Post"
    assert items[0].source_url == "https://example.com/first"
    assert "Hello world from the feed." in items[0].body  # HTML stripped
    assert "<" not in items[0].body
    assert items[0].extra["feed"] == "Test Newsletter"
    assert items[1].title == "Second Post"


def test_rss_limit():
    scraper = RSSScraper(parse=feedparser.parse)
    assert len(list(scraper.scrape(RSS, limit=1))) == 1
