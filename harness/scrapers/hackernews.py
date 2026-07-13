"""
Hacker News scraper — the public Firebase API (no authentication required).

API: https://github.com/HackerNews/API — lists (top/new/best/ask/show) + item trees.
Each story becomes a corpus item: title + link + self-text + top-level comments.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from ..base import BaseScraper, CorpusItem, JsonFetcher, PoliteSession, html_to_text

HN_API = "https://hacker-news.firebaseio.com/v0"
LISTS = {
    "top": "topstories",
    "new": "newstories",
    "best": "beststories",
    "ask": "askstories",
    "show": "showstories",
    "job": "jobstories",
}


def _iso(epoch) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).date().isoformat()
    except Exception:  # noqa: BLE001
        return ""


class HackerNewsScraper(BaseScraper):
    platform = "hackernews"

    def __init__(self, fetch_json: JsonFetcher | None = None, max_comments: int = 10):
        if fetch_json is None:
            fetch_json = PoliteSession(min_interval=0.1).get_json  # HN Firebase is generous
        self.fetch_json = fetch_json
        self.max_comments = max_comments

    def _item(self, item_id):
        return self.fetch_json(f"{HN_API}/item/{item_id}.json")

    def scrape(self, target: str = "top", limit: int = 25) -> Iterable[CorpusItem]:
        target = (target or "top").lower().strip()
        if target.isdigit():
            ids = [int(target)]
        else:
            listname = LISTS.get(target, "topstories")
            ids = (self.fetch_json(f"{HN_API}/{listname}.json") or [])[:limit]
        for sid in ids:
            story = self._item(sid)
            if (
                not story
                or story.get("type") == "comment"
                or story.get("deleted")
                or story.get("dead")
            ):
                continue
            yield self._story_to_item(story)

    def _story_to_item(self, story: dict) -> CorpusItem:
        url = story.get("url", "") or ""
        text = html_to_text(story.get("text", "") or "")
        parts: list[str] = []
        if url:
            parts.append(f"Link: {url}")
        if text:
            parts.append(text)
        comments = self._top_comments(story.get("kids", []) or [])
        if comments:
            parts.append("## Discussion\n\n" + "\n\n".join(comments))
        return CorpusItem(
            platform=self.platform,
            source_url=f"https://news.ycombinator.com/item?id={story.get('id')}",
            title=story.get("title", "") or "",
            author=story.get("by", "") or "",
            date=_iso(story.get("time")),
            body="\n\n".join(p for p in parts if p),
            extra={
                "score": story.get("score"),
                "descendants": story.get("descendants"),
                "external_url": url,
            },
        )

    def _top_comments(self, kids: list) -> list[str]:
        out: list[str] = []
        for cid in kids[: self.max_comments]:
            c = self._item(cid)
            if not c or c.get("deleted") or c.get("dead"):
                continue
            t = html_to_text(c.get("text", "") or "")
            if t:
                out.append(f"- **{c.get('by', '?')}**: {t}")
        return out
