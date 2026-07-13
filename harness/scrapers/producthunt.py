"""
ProductHunt scraper — the official GraphQL API (api.producthunt.com/v2/api/graphql).
This adapter uses Product Hunt's authenticated developer API: set
``PRODUCTHUNT_TOKEN`` (or ``PH_TOKEN``), or pass ``token=``. Without a token the
scraper raises a clear error rather than failing opaquely. Target is a list name
(informational); the query returns top posts by votes.
"""

from __future__ import annotations

import os
from typing import Callable, Iterable

from ..base import BaseScraper, CorpusItem, PoliteSession, html_to_text

PH_GRAPHQL = "https://api.producthunt.com/v2/api/graphql"
QUERY = """
query($n: Int!) {
  posts(first: $n, order: VOTES) {
    edges { node {
      name tagline description url votesCount createdAt
      topics(first: 5) { edges { node { name } } }
    } }
  }
}
""".strip()

# A GraphQL fetcher is (query, variables) -> dict; injected in tests.
GraphQLFetcher = Callable[[str, dict], dict]


class ProductHuntScraper(BaseScraper):
    platform = "producthunt"

    def __init__(self, fetch_graphql: GraphQLFetcher | None = None, token: str | None = None):
        self.token = token or os.environ.get("PRODUCTHUNT_TOKEN") or os.environ.get("PH_TOKEN")
        self._fetch = fetch_graphql

    def _post(self, query: str, variables: dict) -> dict:
        if self._fetch is not None:
            return self._fetch(query, variables)
        if not self.token:
            raise RuntimeError(
                "ProductHunt needs a developer token — set PRODUCTHUNT_TOKEN (or PH_TOKEN), "
                "or pass token=. (Authenticated GraphQL API.)"
            )
        sess = PoliteSession(min_interval=1.0)
        sess.session.headers.update(
            {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        )
        resp = sess.session.post(
            PH_GRAPHQL, json={"query": query, "variables": variables}, timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    def scrape(self, target: str = "featured", limit: int = 20) -> Iterable[CorpusItem]:
        data = self._post(QUERY, {"n": limit})
        edges = (((data.get("data") or {}).get("posts") or {}).get("edges")) or []
        for e in edges[:limit]:
            node = e.get("node") or {}
            item = self._node_to_item(node)
            if item is not None:
                yield item

    def _node_to_item(self, node: dict) -> "CorpusItem | None":
        topics = [
            t["node"]["name"]
            for t in ((node.get("topics") or {}).get("edges") or [])
            if t.get("node", {}).get("name")
        ]
        parts: list[str] = []
        if node.get("tagline"):
            parts.append(node["tagline"])
        if node.get("description"):
            parts.append(html_to_text(node["description"]))
        if topics:
            parts.append("Topics: " + ", ".join(topics))
        body = "\n\n".join(p for p in parts if p)
        return CorpusItem(
            platform=self.platform,
            source_url=node.get("url", "") or "",
            title=node.get("name", "") or "",
            author="",
            date=str(node.get("createdAt", "") or ""),
            body=body,
            extra={"votes": node.get("votesCount"), "topics": topics},
        )
