from harness.scrapers.reddit import RedditScraper

LISTING = {
    "data": {
        "children": [
            {
                "data": {
                    "title": "Self Post",
                    "selftext": "Body &amp; text here.",
                    "author": "alice",
                    "created_utc": 1700000000,
                    "permalink": "/r/test/comments/1/self_post/",
                    "score": 120,
                    "num_comments": 2,
                }
            },
            {
                "data": {
                    "title": "Link Post",
                    "selftext": "",
                    "author": "bob",
                    "created_utc": 1700000100,
                    "permalink": "/r/test/comments/2/link_post/",
                    "url": "https://example.com/x",
                    "score": 5,
                    "num_comments": 0,
                }
            },
            {
                "data": {
                    "title": "Sticky",
                    "stickied": True,
                    "selftext": "x",
                    "permalink": "/r/test/comments/3/",
                }
            },
        ]
    }
}

COMMENTS = [
    {},  # [0] is the post listing, ignored
    {
        "data": {
            "children": [
                {"data": {"author": "carol", "body": "great &amp; useful"}},
                {"data": {"author": "[deleted]", "body": "deleted body"}},
            ]
        }
    },
]


def fake(url):
    if "/comments/" in url:
        return COMMENTS
    if "/hot.json" in url or "/top.json" in url:
        return LISTING
    return None


def test_reddit_listing_with_comments():
    r = RedditScraper(fetch_json=fake, max_comments=8)
    items = list(r.scrape("r/test", limit=25))

    assert [i.title for i in items] == ["Self Post", "Link Post"]  # sticky skipped
    first = items[0]
    assert "Body & text here." in first.body  # entity unescaped
    assert "carol" in first.body and "great & useful" in first.body
    assert "deleted body" not in first.body
    assert first.extra["subreddit"] == "test" and first.extra["score"] == 120
    assert first.date == "2023-11-14"

    assert "Link: https://example.com/x" in items[1].body  # link post w/o selftext


def test_reddit_sort_parse():
    r = RedditScraper(fetch_json=fake)
    assert r._parse_target("r/python/top") == ("python", "top")
    assert r._parse_target("python") == ("python", "hot")
    assert r._parse_target("r/news/bogus") == ("news", "hot")  # unknown sort → hot
