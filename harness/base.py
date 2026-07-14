"""Corpus records, atomic Markdown writes, polite HTTP, and adapter contracts.

The output contract is one Markdown file per item at
``<out>/<platform>/<slug>.md`` with provenance in YAML frontmatter followed by the
title and collected prose.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import requests
import yaml

from .constants import USER_AGENT
from .transport import (
    HttpResponse,
    HttpStatusError,
    ResponseTooLargeError,
    SafeHttpTransport,
)
from .url_safety import (
    UnsafeUrlError,
    redact_sensitive_text,
    sanitize_metadata,
    sanitize_url_for_persistence,
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
    canonical_url: str = ""
    source_profile: str = ""

    def content_hash(self) -> str:
        return hashlib.sha256(self.body.strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WriteResult:
    """Exact outcome for one attempted corpus write."""

    outcome: str
    path: Path | None
    content_hash: str


def write_corpus_item_result(
    item: CorpusItem, out_dir: str | Path, *, scraped_at: str | None = None
) -> WriteResult:
    """
    Write one CorpusItem to ``<out_dir>/<platform>/<slug>.md``.

    Returns an exact written, duplicate, or empty outcome. A duplicate requires
    both the same sanitized source URL and the same verified body hash.
    """
    body = (item.body or "").strip()
    if not body:
        return WriteResult("empty", None, item.content_hash())

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
    safe_source_url = sanitize_url_for_persistence(item.source_url)
    safe_title = str(sanitize_metadata(item.title))
    safe_author = str(sanitize_metadata(item.author))
    safe_date = str(sanitize_metadata(item.date))
    safe_profile = str(sanitize_metadata(item.source_profile))
    slug = slugify(safe_title or safe_source_url)
    path = out / f"{slug}.md"
    chash = item.content_hash()

    front = {
        "platform": item.platform,
        "source_url": safe_source_url,
        "title": safe_title,
        "author": safe_author,
        "date": safe_date,
        "scraped_at": timestamp,
        "content_hash": chash,
    }
    if item.canonical_url:
        front["canonical_url"] = sanitize_url_for_persistence(item.canonical_url)
    if safe_profile:
        front["source_profile"] = safe_profile
    if item.extra:
        front["extra"] = sanitize_metadata(item.extra)
    fm = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()

    doc = f"---\n{fm}\n---\n\n"
    if safe_title:
        doc += f"# {safe_title}\n\n"
    doc += body + "\n"

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=out, prefix=f".{slug}-", suffix=".tmp", delete=False
    ) as handle:
        handle.write(doc)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)

    try:
        # Hard-link publication is atomic and cannot overwrite an existing path,
        # even when concurrent writers selected the same candidate.
        suffix_length = 8
        while True:
            if path.exists() or path.is_symlink():
                existing = None if path.is_symlink() else _existing_identity(path)
                if existing == (safe_source_url, chash):
                    return WriteResult("duplicate", path, chash)
                path = out / f"{slug}-{chash[:suffix_length]}.md"
                suffix_length += 4
                if suffix_length > len(chash):
                    raise RuntimeError(
                        f"could not allocate a collision-safe record path for {slug}"
                    )
                continue
            try:
                os.link(temporary, path)
                return WriteResult("written", path, chash)
            except FileExistsError:
                continue
    finally:
        temporary.unlink(missing_ok=True)


def write_corpus_item(
    item: CorpusItem, out_dir: str | Path, *, scraped_at: str | None = None
) -> Path | None:
    """Backward-compatible wrapper returning only the path written."""

    result = write_corpus_item_result(item, out_dir, scraped_at=scraped_at)
    return result.path if result.outcome == "written" else None


def _existing_identity(path: Path) -> tuple[str, str] | None:
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
        parts = text.split("---", 2)
        if len(parts) < 3:
            return None
        front = yaml.safe_load(parts[1])
        if not isinstance(front, dict):
            return None
        source_url = front.get("source_url")
        content_hash = front.get("content_hash")
        if isinstance(source_url, str) and re.fullmatch(r"[0-9a-f]{64}", str(content_hash)):
            rendered_body = parts[2].lstrip("\n")
            title = front.get("title")
            if isinstance(title, str) and title:
                heading = f"# {title}\n\n"
                if not rendered_body.startswith(heading):
                    return None
                rendered_body = rendered_body[len(heading) :]
            actual_hash = hashlib.sha256(rendered_body.strip().encode("utf-8")).hexdigest()
            if actual_hash != content_hash:
                return None
            return source_url, str(content_hash)
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    return None


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
        self.transport = SafeHttpTransport()

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, url: str, **kw) -> HttpResponse:
        request_headers = dict(self.session.headers)
        request_headers.update(kw.pop("headers", {}) or {})
        timeout = kw.pop("timeout", self.timeout)
        max_bytes = kw.pop("max_bytes", 5 * 1024 * 1024)
        max_redirects = kw.pop("max_redirects", 5)
        if kw:
            unsupported = ", ".join(sorted(kw))
            raise TypeError(f"unsupported protected GET options: {unsupported}")
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                resp = self.transport.get(
                    url,
                    headers=request_headers,
                    timeout=timeout,
                    max_bytes=max_bytes,
                    max_redirects=max_redirects,
                )
                resp.raise_for_status()
                return resp
            except (UnsafeUrlError, ResponseTooLargeError):
                raise
            except HttpStatusError as exc:
                if 400 <= exc.status_code < 500 and exc.status_code not in {408, 429}:
                    raise
                last_err = exc
                time.sleep(min(2**attempt, 5))
            except Exception as e:  # noqa: BLE001 — courteous retry on any transient error
                last_err = e
                time.sleep(min(2**attempt, 5))
        message = f"GET failed after {self.retries + 1} attempts: {url}: {last_err}"
        raise RuntimeError(redact_sensitive_text(message))

    def get_json(self, url: str, **kw):
        return self.get(url, **kw).json()

    def get_text(self, url: str, **kw) -> str:
        return self.get(url, **kw).text

    def robots_allows(self, url: str, *, fail_open: bool = True) -> bool:
        self._throttle()
        return self.transport.robots_allows(url, fail_open=fail_open)


_safe_robots_transport = SafeHttpTransport()


def robots_allows(url: str, user_agent: str = USER_AGENT) -> bool:
    """Return True iff robots.txt permits fetching `url` for `user_agent`.
    Fail-open on an unreachable/missing robots.txt (the courteous default for
    sites that simply don't publish one)."""
    return _safe_robots_transport.robots_allows(url, user_agent=user_agent)


# ---------------------------------------------------------------------------
# BaseScraper
# ---------------------------------------------------------------------------


class BaseScraper(ABC):
    """A platform scraper: turn a `target` into CorpusItems, then write them.

    Subclasses implement `scrape()`; `run()` handles writing + the summary. Tests
    drive `scrape()` directly with injected fetchers, so no network is needed.
    """

    platform: str = "base"
    adapter_schema_version: str = "1"

    @abstractmethod
    def scrape(self, target: str, limit: int = 25) -> Iterable[CorpusItem]: ...

    def acquisition_options(self) -> dict[str, object]:
        """Stable, non-secret options that affect adapter output."""

        return {"adapter_schema_version": self.adapter_schema_version}

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
