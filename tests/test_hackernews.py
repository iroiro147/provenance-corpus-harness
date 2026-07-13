from harness.scrapers.hackernews import HackerNewsScraper

API = "https://hacker-news.firebaseio.com/v0"

FIXTURE = {
    f"{API}/topstories.json": [1, 2, 99],
    f"{API}/item/1.json": {
        "id": 1,
        "type": "story",
        "title": "Story One",
        "by": "alice",
        "time": 1700000000,
        "url": "https://example.com/1",
        "score": 42,
        "descendants": 2,
        "kids": [10, 11],
    },
    f"{API}/item/2.json": {
        "id": 2,
        "type": "story",
        "title": "Ask HN: Thoughts?",
        "by": "bob",
        "time": 1700000100,
        "text": "<p>self &amp; text</p>",
        "score": 10,
        "kids": [],
    },
    f"{API}/item/99.json": {"id": 99, "type": "comment", "by": "eve", "text": "stray comment"},
    f"{API}/item/10.json": {
        "id": 10,
        "type": "comment",
        "by": "carol",
        "text": "<i>nice</i> point",
    },
    f"{API}/item/11.json": {
        "id": 11,
        "type": "comment",
        "by": "dave",
        "text": "agreed",
        "deleted": True,
    },
}


def fake_fetch(url):
    return FIXTURE.get(url)


def test_hackernews_list_scrape_builds_items_with_comments():
    hn = HackerNewsScraper(fetch_json=fake_fetch, max_comments=5)
    items = list(hn.scrape("top", limit=3))

    # id 99 is a comment → skipped; 2 stories remain.
    assert [i.title for i in items] == ["Story One", "Ask HN: Thoughts?"]

    one = items[0]
    assert one.author == "alice"
    assert one.date == "2023-11-14"
    assert one.source_url == "https://news.ycombinator.com/item?id=1"
    assert "Link: https://example.com/1" in one.body
    assert "carol" in one.body and "nice point" in one.body  # comment included, HTML stripped
    assert "dave" not in one.body  # deleted comment dropped
    assert one.extra["score"] == 42

    two = items[1]
    assert "self & text" in two.body  # self-text, entity unescaped


def test_hackernews_single_id():
    hn = HackerNewsScraper(fetch_json=fake_fetch)
    items = list(hn.scrape("1", limit=25))
    assert len(items) == 1 and items[0].title == "Story One"
