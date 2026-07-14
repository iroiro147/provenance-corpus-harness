"""
GitHub scraper — the public REST API. Gathers a repo's README + description/topics +
recent release notes (corpus-relevant text). Unauthenticated access supports light use
(60 requests/hour); set ``GITHUB_TOKEN`` for a higher documented API allowance.

Target: `owner/repo` (comma-separated for several).
"""

from __future__ import annotations

import base64
import os
from typing import Iterable

from ..base import BaseScraper, CorpusItem, JsonFetcher, PoliteSession

GH_API = "https://api.github.com"


class GitHubScraper(BaseScraper):
    platform = "github"

    def __init__(
        self, fetch_json: JsonFetcher | None = None, token: str | None = None, max_releases: int = 3
    ):
        self.token = token or os.environ.get("GITHUB_TOKEN")
        if fetch_json is None:
            sess = PoliteSession(min_interval=0.5)
            sess.session.headers.update({"Accept": "application/vnd.github+json"})
            if self.token:
                sess.session.headers.update({"Authorization": f"Bearer {self.token}"})
            fetch_json = sess.get_json
        self.fetch_json = fetch_json
        self.max_releases = max_releases

    def acquisition_options(self) -> dict[str, object]:
        return {
            **super().acquisition_options(),
            "authenticated": bool(self.token),
            "max_releases": self.max_releases,
        }

    def scrape(self, target: str, limit: int = 10) -> Iterable[CorpusItem]:
        repos = [r.strip().strip("/") for r in (target or "").split(",") if r.strip()][
            : max(limit, 1)
        ]
        for repo in repos:
            item = self.fetch_repo(repo)
            if item is not None:
                yield item

    def fetch_repo(self, owner_repo: str) -> "CorpusItem | None":
        if "/" not in owner_repo:
            return None
        try:
            meta = self.fetch_json(f"{GH_API}/repos/{owner_repo}") or {}
        except Exception:  # noqa: BLE001 — 404/403/rate-limit → skip gracefully
            return None
        if not meta or meta.get("message") == "Not Found":
            return None

        parts: list[str] = []
        if meta.get("description"):
            parts.append(meta["description"])
        topics = meta.get("topics") or []
        if topics:
            parts.append("Topics: " + ", ".join(topics))
        readme = self._readme(owner_repo)
        if readme:
            parts.append("## README\n\n" + readme)
        releases = self._releases(owner_repo)
        if releases:
            parts.append("## Recent releases\n\n" + "\n\n".join(releases))

        return CorpusItem(
            platform=self.platform,
            source_url=meta.get("html_url", f"https://github.com/{owner_repo}"),
            title=meta.get("full_name", owner_repo),
            author=(meta.get("owner") or {}).get("login", "") or "",
            date=str(meta.get("pushed_at", "") or ""),
            body="\n\n".join(p for p in parts if p),
            extra={
                "stars": meta.get("stargazers_count"),
                "language": meta.get("language"),
                "topics": topics,
            },
        )

    def _readme(self, owner_repo: str) -> str:
        try:
            data = self.fetch_json(f"{GH_API}/repos/{owner_repo}/readme") or {}
        except Exception:  # noqa: BLE001
            return ""
        content = data.get("content", "") or ""
        if data.get("encoding") == "base64" and content:
            try:
                return base64.b64decode(content).decode("utf-8", "ignore").strip()
            except Exception:  # noqa: BLE001
                return ""
        return content.strip()

    def _releases(self, owner_repo: str) -> list[str]:
        try:
            rels = (
                self.fetch_json(
                    f"{GH_API}/repos/{owner_repo}/releases?per_page={self.max_releases}"
                )
                or []
            )
        except Exception:  # noqa: BLE001
            return []
        out: list[str] = []
        for r in (rels if isinstance(rels, list) else [])[: self.max_releases]:
            name = r.get("name") or r.get("tag_name") or ""
            body = (r.get("body") or "").strip()
            if name or body:
                out.append(f"### {name}\n\n{body}".strip())
        return out
