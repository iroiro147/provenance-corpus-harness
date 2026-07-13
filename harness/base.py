"""Corpus records, atomic Markdown writes, polite HTTP, and adapter contracts.

The output contract is one Markdown file per item at
``<out>/<platform>/<slug>.md`` with provenance in YAML frontmatter followed by the
title and collected prose.
"""

from __future__ import annotations

import hashlib
import html
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
import yaml

USER_AGENT = (
    "provenance-corpus-harness/0.1 (policy-conscious corpus collection; "
    "+https://github.com/iroiro147/provenance-corpus-harness)"
)

# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\f\r]+")
_MULTINL_RE = re.compile(r"\n{3,}")
_PLATFORM_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")


def html_to_text(s: str) -> str:
    """Strip HTML/XML tags + unescape entities → clean prose (for RSS/HTML bodies)."""
    if not s:
        return ""
    s = re.sub(r"<script[\s\S]*?</script>", " ", s, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", s, flags=re.I)
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s)
    s = _MULTINL_RE.sub("\n\n", s)
    return s.strip()


def slugify(s: str, maxlen: int = 80) -> str:
    """Filesystem-safe, lowercase, hyphenated slug."""
    s = (s or "item").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if len(s) > maxlen:
        s = s[:maxlen].rstrip("-")
    return s or "item"


# ---------------------------------------------------------------------------
# Corpus item + writer
# ---------------------------------------------------------------------------


@dataclass
class CorpusItem:
    """One gathered unit of content and its source provenance."""

    platform: str
    source_url: str
    title: str = ""
    author: str = ""
    date: str = ""  # ISO-ish date of the *content* (not scrape time)
    body: str = ""  # the prose (clean text / light markdown)
    extra: dict = field(default_factory=dict)  # platform metadata (engagement, ids…)

    def content_hash(self) -> str:
        return hashlib.sha256(self.body.strip().encode("utf-8")).hexdigest()


def write_corpus_item(
    item: CorpusItem, out_dir: str | Path, *, scraped_at: str | None = None
) -> Path | None:
    """
    Write one CorpusItem to ``<out_dir>/<platform>/<slug>.md``.

    Returns the path written, or None when skipped (empty body, or an identical
    content_hash already on disk at that slug (light idempotent dedup).
    """
    body = (item.body or "").strip()
    if not body:
        return None

    if not isinstance(item.platform, str) or not _PLATFORM_RE.fullmatch(item.platform):
        raise ValueError(
            "platform must be a lowercase 1-64 character identifier containing only "
            "letters, digits, underscores, or hyphens"
        )

    timestamp = _scraped_at_timestamp(scraped_at)
    root = Path(out_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    out = (root / item.platform).resolve()
    if out.parent != root:
        raise ValueError("platform output directory escapes the configured output root")
    out.mkdir(parents=True, exist_ok=True)
    slug = slugify(item.title or item.source_url)
    path = out / f"{slug}.md"
    chash = item.content_hash()

    # Idempotent dedup: same slug + same content already written → skip.
    if path.exists():
        existing = path.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"^content_hash:\s*([0-9a-f]{64})", existing, flags=re.M)
        if m and m.group(1) == chash:
            return None
        # Same slug, different content → disambiguate by a short hash suffix.
        path = out / f"{slug}-{chash[:8]}.md"

    front = {
        "platform": item.platform,
        "source_url": item.source_url,
        "title": item.title,
        "author": item.author,
        "date": item.date,
        "scraped_at": timestamp,
        "content_hash": chash,
    }
    if item.extra:
        front["extra"] = item.extra
    fm = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()

    doc = f"---\n{fm}\n---\n\n"
    if item.title:
        doc += f"# {item.title}\n\n"
    doc += body + "\n"

    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(doc, encoding="utf-8")
    tmp.replace(path)  # atomic
    return path


def _scraped_at_timestamp(value: str | None) -> str:
    """Return a timezone-aware ISO 8601 collection timestamp.

    Library callers receive a UTC timestamp by default, matching the CLI's
    provenance contract. Explicit timestamps must include a timezone so a
    record cannot silently carry ambiguous collection time.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return datetime.now(timezone.utc).isoformat()
    if not isinstance(value, str):
        raise TypeError("scraped_at must be a timezone-aware ISO 8601 string or None")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("scraped_at must be a timezone-aware ISO 8601 string") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("scraped_at must include a timezone offset")
    return value.strip()


# ---------------------------------------------------------------------------
# Polite HTTP + robots
# ---------------------------------------------------------------------------


class PoliteSession:
    """A requests session with a fixed UA, a minimum inter-request interval, and a
    small retry — the courteous baseline for an operator-authorized fetch."""

    def __init__(self, min_interval: float = 0.5, timeout: float = 20.0, retries: int = 2):
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, url: str, **kw) -> requests.Response:
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout, **kw)
                resp.raise_for_status()
                return resp
            except Exception as e:  # noqa: BLE001 — courteous retry on any transient error
                last_err = e
                time.sleep(min(2**attempt, 5))
        raise RuntimeError(f"GET failed after {self.retries + 1} attempts: {url}: {last_err}")

    def get_json(self, url: str, **kw):
        return self.get(url, **kw).json()

    def get_text(self, url: str, **kw) -> str:
        return self.get(url, **kw).text


_robots_cache: dict[str, RobotFileParser | None] = {}


def robots_allows(url: str, user_agent: str = USER_AGENT) -> bool:
    """Return True iff robots.txt permits fetching `url` for `user_agent`.
    Fail-open on an unreachable/missing robots.txt (the courteous default for
    sites that simply don't publish one)."""
    try:
        parts = urlparse(url)
        if not parts.scheme or not parts.netloc:
            return True
        base = f"{parts.scheme}://{parts.netloc}"
        rp = _robots_cache.get(base, "__miss__")  # type: ignore[arg-type]
        if rp == "__miss__":
            parser = RobotFileParser()
            parser.set_url(f"{base}/robots.txt")
            try:
                parser.read()
                rp = parser
            except Exception:  # noqa: BLE001
                rp = None  # unreachable robots → fail-open
            _robots_cache[base] = rp
        if rp is None:
            return True
        return rp.can_fetch(user_agent, url)
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------------------
# BaseScraper
# ---------------------------------------------------------------------------


class BaseScraper(ABC):
    """A platform scraper: turn a `target` into CorpusItems, then write them.

    Subclasses implement `scrape()`; `run()` handles writing + the summary. Tests
    drive `scrape()` directly with injected fetchers, so no network is needed.
    """

    platform: str = "base"

    @abstractmethod
    def scrape(self, target: str, limit: int = 25) -> Iterable[CorpusItem]: ...

    def run(
        self,
        target: str,
        out_dir: str | Path,
        *,
        limit: int = 25,
        scraped_at: str | None = None,
    ) -> list[Path]:
        timestamp = _scraped_at_timestamp(scraped_at)
        written: list[Path] = []
        for item in self.scrape(target, limit=limit):
            p = write_corpus_item(item, out_dir, scraped_at=timestamp)
            if p is not None:
                written.append(p)
        return written


# A fetcher is any callable url -> parsed-json (injected in tests).
JsonFetcher = Callable[[str], object]
