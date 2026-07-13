"""
Generic blog/article scraper — trafilatura extracts the main article text from an
operator-supplied URL. Robots.txt is checked by default (an explicitly disallowed URL
is skipped, not fetched). Target is one article URL, or a comma-separated list.
"""

from __future__ import annotations

from typing import Callable, Iterable

import trafilatura

from ..base import BaseScraper, CorpusItem, robots_allows


class BlogScraper(BaseScraper):
    platform = "blog"

    def __init__(
        self, fetch_html: Callable[[str], "str | None"] | None = None, respect_robots: bool = True
    ):
        # `fetch_html` is injectable (tests pass downloaded HTML directly).
        self.fetch_html = fetch_html or trafilatura.fetch_url
        self.respect_robots = respect_robots

    def scrape(self, target: str, limit: int = 10) -> Iterable[CorpusItem]:
        urls = [u.strip() for u in (target or "").split(",") if u.strip()][: max(limit, 1)]
        for url in urls:
            item = self.extract(url)
            if item is not None:
                yield item

    def extract(self, url: str, downloaded: "str | None" = None) -> "CorpusItem | None":
        if downloaded is None:
            if self.respect_robots and not robots_allows(url):
                return None  # robots.txt disallows — skip, don't fetch
            downloaded = self.fetch_html(url)
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded, include_comments=False, favor_recall=True)
        if not text or not text.strip():
            return None
        title = author = date = ""
        try:
            meta = trafilatura.extract_metadata(downloaded)
            if meta is not None:
                title = getattr(meta, "title", "") or ""
                author = getattr(meta, "author", "") or ""
                date = getattr(meta, "date", "") or ""
        except Exception:  # noqa: BLE001 — metadata is best-effort
            pass
        return CorpusItem(
            platform=self.platform,
            source_url=url,
            title=title or url,
            author=author,
            date=date,
            body=text.strip(),
        )
