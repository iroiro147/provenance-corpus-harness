import pytest

from harness.scrapers.producthunt import ProductHuntScraper

RESP = {
    "data": {
        "posts": {
            "edges": [
                {
                    "node": {
                        "name": "CoolApp",
                        "tagline": "Does cool things",
                        "description": "A <b>great</b> tool.",
                        "url": "https://www.producthunt.com/posts/coolapp",
                        "votesCount": 250,
                        "createdAt": "2026-06-01T00:00:00Z",
                        "topics": {
                            "edges": [{"node": {"name": "Productivity"}}, {"node": {"name": "AI"}}]
                        },
                    }
                },
            ]
        }
    }
}


def test_producthunt_offline():
    ph = ProductHuntScraper(fetch_graphql=lambda q, v: RESP)
    items = list(ph.scrape("featured", limit=10))

    assert len(items) == 1
    it = items[0]
    assert it.title == "CoolApp"
    assert "Does cool things" in it.body
    assert "great tool" in it.body  # HTML stripped from description
    assert "Topics: Productivity, AI" in it.body
    assert it.extra["votes"] == 250
    assert it.source_url == "https://www.producthunt.com/posts/coolapp"


def test_producthunt_requires_token():
    ph = ProductHuntScraper()
    ph.token = None  # force the no-token path regardless of environment
    with pytest.raises(RuntimeError):
        list(ph.scrape("featured"))
