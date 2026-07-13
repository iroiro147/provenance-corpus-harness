"""
RSS/Atom scraper — publisher-provided structured feeds. Covers Substack
(`<name>.substack.com/feed`), Medium
(`medium.com/feed/@user`), and any blog/newsletter RSS/Atom URL.
"""

from __future__ import annotations

from typing import Callable, Iterable

import feedparser

from ..base import BaseScraper, CorpusItem, html_to_text


class RSSScraper(BaseScraper):
    platform = "rss"

    def __init__(self, parse: Callable | None = None, platform_name: str = "rss"):
        # `parse` is injectable (tests pass an RSS string); feedparser.parse accepts a
        # URL, a raw string, or a file path.
        self.parse = parse or feedparser.parse
        self.platform = platform_name

    def scrape(self, target: str, limit: int = 25) -> Iterable[CorpusItem]:
        feed = self.parse(target)
        feed_meta = getattr(feed, "feed", {}) or {}
        feed_title = feed_meta.get("title", "") if hasattr(feed_meta, "get") else ""
        for entry in (getattr(feed, "entries", []) or [])[:limit]:
            body = ""
            content = entry.get("content") if hasattr(entry, "get") else None
            if content:
                body = html_to_text(content[0].get("value", ""))
            if not body:
                body = html_to_text(entry.get("summary", "") or entry.get("description", ""))
            tags = (
                [t.get("term", "") for t in entry.get("tags", []) or []]
                if entry.get("tags")
                else []
            )
            yield CorpusItem(
                platform=self.platform,
                source_url=entry.get("link", "") or "",
                title=entry.get("title", "") or "",
                author=entry.get("author", "") or feed_title,
                date=entry.get("published", "") or entry.get("updated", "") or "",
                body=body,
                extra={"feed": feed_title, "tags": [t for t in tags if t]},
            )
