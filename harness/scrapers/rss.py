"""
RSS/Atom scraper — publisher-provided structured feeds. Covers Substack
(`<name>.substack.com/feed`), Medium
(`medium.com/feed/@user`), and any blog/newsletter RSS/Atom URL.
"""

from __future__ import annotations

from typing import Callable, Iterable

import feedparser

from ..base import BaseScraper, CorpusItem, html_to_text
from ..transport import SafeHttpTransport, TransportError


class RSSScraper(BaseScraper):
    platform = "rss"

    def __init__(
        self,
        parse: Callable | None = None,
        platform_name: str = "rss",
        transport: SafeHttpTransport | None = None,
    ):
        # `parse` is injectable (tests pass an RSS string); feedparser.parse accepts a
        # URL, a raw string, or a file path.
        self.parse = parse or feedparser.parse
        self._uses_default_parser = parse is None
        self.transport = transport or SafeHttpTransport()
        self.platform = platform_name

    def scrape(self, target: str, limit: int = 25) -> Iterable[CorpusItem]:
        source: object = target
        if self._uses_default_parser and target.lower().startswith(("http://", "https://")):
            response = self.transport.get(target)
            response.raise_for_status()
            if response.media_type and not (
                response.media_type.startswith("text/")
                or "xml" in response.media_type
                or response.media_type in {"application/rss+xml", "application/atom+xml"}
            ):
                raise TransportError(
                    f"feed response has unsupported media type: {response.media_type}"
                )
            source = response.body
        feed = self.parse(source)
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
