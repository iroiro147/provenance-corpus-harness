"""
Reddit scraper — the public JSON endpoints (append `.json` to any listing or permalink).
No OAuth for light, *identified* use; a descriptive User-Agent (PoliteSession) is required
and we throttle politely. For heavy/production volume, switch to PRAW with OAuth creds
(documented in the README) — same CorpusItem output.

Target: `r/<sub>` | `<sub>` | `r/<sub>/<sort>`  (sort ∈ hot|new|top|best|rising|controversial).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from ..base import BaseScraper, CorpusItem, JsonFetcher, PoliteSession, html_to_text

REDDIT = "https://www.reddit.com"
SORTS = {"hot", "new", "top", "best", "rising", "controversial"}


def _iso(epoch) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).date().isoformat()
    except Exception:  # noqa: BLE001
        return ""


class RedditScraper(BaseScraper):
    platform = "reddit"

    def __init__(self, fetch_json: JsonFetcher | None = None, max_comments: int = 8):
        if fetch_json is None:
            fetch_json = PoliteSession(min_interval=1.0).get_json  # be gentle with Reddit
        self.fetch_json = fetch_json
        self.max_comments = max_comments

    def acquisition_options(self) -> dict[str, object]:
        return {**super().acquisition_options(), "max_comments": self.max_comments}

    def _parse_target(self, target: str) -> "tuple[str, str]":
        t = (target or "").strip().strip("/")
        if t.lower().startswith("r/"):
            t = t[2:]
        parts = [p for p in t.split("/") if p]
        sub = parts[0] if parts else "popular"
        sort = parts[1] if len(parts) > 1 and parts[1] in SORTS else "hot"
        return sub, sort

    def scrape(self, target: str, limit: int = 25) -> Iterable[CorpusItem]:
        import sys

        sub, sort = self._parse_target(target)
        try:
            data = self.fetch_json(f"{REDDIT}/r/{sub}/{sort}.json?limit={limit}&raw_json=1") or {}
        except Exception as e:  # noqa: BLE001 — Reddit commonly 403s the public JSON
            print(
                f"[reddit] public JSON unavailable ({e}). Reddit increasingly gates this "
                f"endpoint — use OAuth/PRAW credentials for reliable access (see README). "
                f"The adapter will not spoof a browser or bypass the block.",
                file=sys.stderr,
            )
            return
        children = ((data.get("data") or {}).get("children")) or []
        for ch in children[:limit]:
            post = ch.get("data") or {}
            if post.get("stickied"):
                continue
            item = self._post_to_item(sub, post)
            if item is not None:
                yield item

    def _post_to_item(self, sub: str, post: dict) -> "CorpusItem | None":
        selftext = html_to_text(post.get("selftext", "") or "")
        url = post.get("url", "") or ""
        permalink = post.get("permalink", "") or ""
        parts: list[str] = []
        if selftext:
            parts.append(selftext)
        elif url and not url.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".gifv")):
            parts.append(f"Link: {url}")
        comments = self._top_comments(permalink) if permalink else []
        if comments:
            parts.append("## Top comments\n\n" + "\n\n".join(comments))
        body = "\n\n".join(p for p in parts if p)
        return CorpusItem(
            platform=self.platform,
            source_url=f"{REDDIT}{permalink}" if permalink else url,
            title=post.get("title", "") or "",
            author=post.get("author", "") or "",
            date=_iso(post.get("created_utc")),
            body=body,
            extra={
                "subreddit": sub,
                "score": post.get("score"),
                "num_comments": post.get("num_comments"),
            },
        )

    def _top_comments(self, permalink: str) -> list[str]:
        try:
            data = self.fetch_json(f"{REDDIT}{permalink}.json?limit={self.max_comments}&raw_json=1")
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(data, list) or len(data) < 2:
            return []
        out: list[str] = []
        for ch in ((data[1].get("data") or {}).get("children") or [])[: self.max_comments]:
            c = ch.get("data") or {}
            body = html_to_text(c.get("body", "") or "")
            author = c.get("author")
            if body and author not in (None, "[deleted]"):
                out.append(f"- **{author}**: {body}")
        return out
