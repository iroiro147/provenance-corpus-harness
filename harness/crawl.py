"""Bounded, provenance-friendly crawl frontier primitives.

The frontier is deliberately independent from HTTP and filesystem code.  Callers
fetch robots.txt and pages through the protected transport, then feed discovered
links back through this module.  That keeps URL identity, scope, and breadth-first
ordering testable without a live network.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import re
import tempfile
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import trafilatura

from .base import CorpusItem, html_to_text, write_corpus_item_result
from .browser import BrowserDriver, BrowserPolicy, BrowserRenderFailure, render_page
from .constants import USER_AGENT
from .transport import SafeHttpTransport
from .url_safety import (
    DEFAULT_TRACKING_QUERY_KEYS,
    UnsafeUrlError,
    assert_safe_public_url,
    canonicalize_url,
    redact_sensitive_text,
)


class FrontierStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class FrontierSkipReason(str, Enum):
    INVALID_URL = "invalid_url"
    CROSS_ORIGIN = "cross_origin"
    MAX_DEPTH = "max_depth"
    ROBOTS_DISALLOWED = "robots_disallowed"
    ROBOTS_UNAVAILABLE = "robots_unavailable"
    POLICY_DENIED = "policy_denied"
    FRONTIER_LIMIT = "frontier_limit"


@dataclass(frozen=True)
class CrawlPolicy:
    """Hard crawl limits and URL-scope rules.

    Cross-origin traversal is opt-in per origin.  ``allow_patterns`` and
    ``deny_patterns`` are shell-style globs over canonical URLs; deny wins.
    Robots failures are fail-closed by default for a multi-page crawl.
    """

    max_pages: int = 25
    max_depth: int = 1
    max_frontier_entries: int = 1_000
    max_links_per_page: int = 500
    same_origin: bool = True
    allowed_origins: tuple[str, ...] = ()
    allow_patterns: tuple[str, ...] = ()
    deny_patterns: tuple[str, ...] = ()
    strip_query_params: tuple[str, ...] = DEFAULT_TRACKING_QUERY_KEYS
    respect_robots: bool = True
    robots_fail_closed: bool = True
    allowed_content_types: tuple[str, ...] = (
        "text/html",
        "application/xhtml+xml",
        "text/markdown",
    )
    max_page_bytes: int = 5 * 1024 * 1024
    max_total_bytes: int = 50 * 1024 * 1024
    min_interval: float = 0.5
    timeout_seconds: float = 20.0
    max_redirects: int = 0

    def __post_init__(self) -> None:
        if self.max_pages <= 0:
            raise ValueError("max_pages must be positive")
        if self.max_depth < 0:
            raise ValueError("max_depth must not be negative")
        if self.max_frontier_entries <= 0:
            raise ValueError("max_frontier_entries must be positive")
        if self.max_links_per_page <= 0:
            raise ValueError("max_links_per_page must be positive")
        if not self.same_origin and not self.allowed_origins:
            raise ValueError("cross-origin crawling requires explicit allowed_origins")
        if any(not pattern or len(pattern) > 512 for pattern in self.allow_patterns):
            raise ValueError("allow patterns must contain 1-512 characters")
        if any(not pattern or len(pattern) > 512 for pattern in self.deny_patterns):
            raise ValueError("deny patterns must contain 1-512 characters")
        if not self.allowed_content_types:
            raise ValueError("allowed_content_types must not be empty")
        if self.max_page_bytes <= 0 or self.max_total_bytes <= 0:
            raise ValueError("crawl byte budgets must be positive")
        if self.max_page_bytes > self.max_total_bytes:
            raise ValueError("max_page_bytes must not exceed max_total_bytes")
        if self.min_interval < 0:
            raise ValueError("min_interval must not be negative")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_redirects != 0:
            raise ValueError(
                "crawl redirects require an every-hop origin validator; use zero for now"
            )


@dataclass(frozen=True)
class FrontierEntry:
    canonical_url: str
    depth: int
    status: FrontierStatus
    parent_url: str | None = None
    skip_reason: FrontierSkipReason | None = None
    error: str = ""

    @property
    def origin(self) -> str:
        return _origin(self.canonical_url)


RobotsChecker = Callable[[str], bool]


@dataclass(frozen=True)
class SitePageRecord:
    url: str
    depth: int
    outcome: str
    status_code: int = 0
    content_type: str = ""
    byte_size: int = 0
    content_hash: str = ""
    title: str = ""
    path: Path | None = None
    links_discovered: int = 0
    source_profile: str = "static"
    error: str = ""


@dataclass(frozen=True)
class SiteCollectionResult:
    seed: str
    policy: CrawlPolicy
    status: str
    written: int
    duplicates: int
    empty: int
    failed: int
    fetched_pages: int
    total_bytes: int
    paths: tuple[Path, ...]
    pages: tuple[SitePageRecord, ...]
    frontier_events: tuple[FrontierEntry, ...]
    frontier_exhausted: bool
    page_limit_reached: bool = False


@dataclass(frozen=True)
class SiteReceiptVerification:
    ok: bool
    checked: int
    errors: tuple[str, ...]


@dataclass
class _CrawlRequestLedger:
    """One crawl-wide byte and politeness budget, including robots reads."""

    policy: CrawlPolicy
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    total_bytes: int = 0
    last_request_at: float | None = None

    @property
    def remaining_bytes(self) -> int:
        return max(0, self.policy.max_total_bytes - self.total_bytes)

    def before_request(self) -> None:
        now = self.clock()
        if self.last_request_at is not None:
            wait = self.policy.min_interval - (now - self.last_request_at)
            if wait > 0:
                self.sleep(wait)
                now += wait
        self.last_request_at = now

    def add_bytes(self, byte_count: int) -> None:
        if byte_count < 0:
            raise ValueError("crawl byte count must not be negative")
        if byte_count > self.remaining_bytes:
            raise ValueError("crawl total byte budget exceeded")
        self.total_bytes += byte_count


class _RobotsGate:
    """Fetch and cache robots rules through the crawl-wide request ledger."""

    _MAX_ROBOTS_BYTES = 512 * 1024

    def __init__(
        self,
        transport: SafeHttpTransport,
        policy: CrawlPolicy,
        ledger: _CrawlRequestLedger,
    ) -> None:
        self.transport = transport
        self.policy = policy
        self.ledger = ledger
        self._cache: dict[str, RobotFileParser | bool] = {}

    def allows(self, url: str) -> bool:
        safe_url = assert_safe_public_url(url)
        origin = _origin(safe_url)
        cached = self._cache.get(origin)
        if cached is not None:
            return cached if isinstance(cached, bool) else cached.can_fetch(USER_AGENT, safe_url)

        try:
            remaining = self.ledger.remaining_bytes
            if remaining <= 0:
                raise ValueError("crawl total byte budget exhausted before robots fetch")
            self.ledger.before_request()
            response = self.transport.get(
                f"{origin}/robots.txt",
                timeout=self.policy.timeout_seconds,
                max_bytes=min(self._MAX_ROBOTS_BYTES, remaining),
                max_redirects=self.policy.max_redirects,
            )
            self.ledger.add_bytes(len(response.body))
            if response.status_code in {404, 410}:
                self._cache[origin] = True
                return True
            response.raise_for_status()
            parser = RobotFileParser()
            parser.set_url(f"{origin}/robots.txt")
            parser.parse(response.text.splitlines())
            self._cache[origin] = parser
            return parser.can_fetch(USER_AGENT, safe_url)
        except Exception:
            if self.policy.robots_fail_closed:
                raise
            self._cache[origin] = True
            return True


@dataclass
class CrawlFrontier:
    """A bounded breadth-first frontier with canonical URL identity."""

    seed: str
    policy: CrawlPolicy = field(default_factory=CrawlPolicy)
    robots_checker: RobotsChecker | None = None
    _entries: dict[str, FrontierEntry] = field(init=False, default_factory=dict)
    _queue: deque[str] = field(init=False, default_factory=deque)
    _events: list[FrontierEntry] = field(init=False, default_factory=list)
    _started: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        seed = canonicalize_url(self.seed, strip_query_params=self.policy.strip_query_params)
        self.seed = seed
        self._seed_origin = _origin(seed)
        self._allowed_origins = {
            _normalize_origin(origin) for origin in self.policy.allowed_origins
        }
        self._allowed_origins.add(self._seed_origin)
        self.discover([seed], parent_url=None, depth=0)

    @property
    def entries(self) -> tuple[FrontierEntry, ...]:
        return tuple(self._entries.values())

    @property
    def events(self) -> tuple[FrontierEntry, ...]:
        """Immutable snapshots suitable for an append-only JSONL ledger."""

        return tuple(self._events)

    @property
    def exhausted(self) -> bool:
        return not self._queue

    @property
    def page_limit_reached(self) -> bool:
        return self._started >= self.policy.max_pages and bool(self._queue)

    def discover(
        self,
        links: Iterable[str],
        *,
        parent_url: str | None,
        depth: int,
    ) -> tuple[FrontierEntry, ...]:
        """Canonicalize, classify, and enqueue previously unseen links."""

        discovered: list[FrontierEntry] = []
        base_url = parent_url or self.seed
        for index, raw_url in enumerate(links):
            if index >= self.policy.max_links_per_page:
                break
            entry = self._classify(
                str(raw_url), base_url=base_url, parent_url=parent_url, depth=depth
            )
            if entry is None:
                continue
            discovered.append(entry)
            if entry.skip_reason is FrontierSkipReason.FRONTIER_LIMIT:
                # Preserve the bounded return signal without defeating the
                # in-memory frontier cap by retaining every excess URL.
                continue
            self._record(entry)
            if entry.status is FrontierStatus.QUEUED:
                self._queue.append(entry.canonical_url)
        return tuple(discovered)

    def pop(self) -> FrontierEntry | None:
        if self._started >= self.policy.max_pages:
            return None
        while self._queue:
            url = self._queue.popleft()
            entry = self._entries[url]
            if entry.status is not FrontierStatus.QUEUED:
                continue
            self._started += 1
            return self._transition(url, FrontierStatus.IN_PROGRESS)
        return None

    def mark_done(self, url: str) -> FrontierEntry:
        return self._transition(url, FrontierStatus.DONE)

    def mark_failed(self, url: str, error: str) -> FrontierEntry:
        return self._transition(
            url, FrontierStatus.FAILED, error=redact_sensitive_text(str(error))[:1_000]
        )

    def _classify(
        self,
        raw_url: str,
        *,
        base_url: str,
        parent_url: str | None,
        depth: int,
    ) -> FrontierEntry | None:
        try:
            canonical = canonicalize_url(
                raw_url,
                base_url=base_url,
                strip_query_params=self.policy.strip_query_params,
            )
        except (UnsafeUrlError, ValueError):
            # Unsafe or malformed URLs must not be persisted in frontier ledgers.
            return None
        if canonical in self._entries:
            return None
        if len(self._entries) >= self.policy.max_frontier_entries:
            return FrontierEntry(
                canonical,
                depth,
                FrontierStatus.SKIPPED,
                parent_url,
                FrontierSkipReason.FRONTIER_LIMIT,
            )
        if depth > self.policy.max_depth:
            return FrontierEntry(
                canonical,
                depth,
                FrontierStatus.SKIPPED,
                parent_url,
                FrontierSkipReason.MAX_DEPTH,
            )
        origin = _origin(canonical)
        if origin not in self._allowed_origins:
            return FrontierEntry(
                canonical,
                depth,
                FrontierStatus.SKIPPED,
                parent_url,
                FrontierSkipReason.CROSS_ORIGIN,
            )
        if _matches_any(canonical, self.policy.deny_patterns) or (
            self.policy.allow_patterns and not _matches_any(canonical, self.policy.allow_patterns)
        ):
            return FrontierEntry(
                canonical,
                depth,
                FrontierStatus.SKIPPED,
                parent_url,
                FrontierSkipReason.POLICY_DENIED,
            )
        if self.policy.respect_robots and self.robots_checker is not None:
            try:
                allowed = self.robots_checker(canonical)
            except Exception:  # noqa: BLE001 - policy converts transport failure to a state
                if self.policy.robots_fail_closed:
                    return FrontierEntry(
                        canonical,
                        depth,
                        FrontierStatus.SKIPPED,
                        parent_url,
                        FrontierSkipReason.ROBOTS_UNAVAILABLE,
                    )
                allowed = True
            if not allowed:
                return FrontierEntry(
                    canonical,
                    depth,
                    FrontierStatus.SKIPPED,
                    parent_url,
                    FrontierSkipReason.ROBOTS_DISALLOWED,
                )
        return FrontierEntry(canonical, depth, FrontierStatus.QUEUED, parent_url)

    def _record(self, entry: FrontierEntry) -> None:
        self._entries[entry.canonical_url] = entry
        self._events.append(entry)

    def _transition(self, url: str, status: FrontierStatus, *, error: str = "") -> FrontierEntry:
        canonical = canonicalize_url(
            url,
            base_url=self.seed,
            strip_query_params=self.policy.strip_query_params,
        )
        current = self._entries.get(canonical)
        if current is None:
            raise KeyError(f"frontier URL is unknown: {canonical}")
        if current.status not in {FrontierStatus.QUEUED, FrontierStatus.IN_PROGRESS}:
            raise ValueError(f"cannot transition {current.status.value} frontier entry")
        updated = replace(current, status=status, error=error)
        self._entries[canonical] = updated
        self._events.append(updated)
        return updated


def collect_site(
    seed: str,
    out_dir: str | Path,
    *,
    policy: CrawlPolicy | None = None,
    transport: SafeHttpTransport | None = None,
    renderer: BrowserDriver | None = None,
    browser_policy: BrowserPolicy | None = None,
    scraped_at: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> SiteCollectionResult:
    """Collect a bounded authorized site into provenance-rich Markdown.

    Static pages are fetched through ``SafeHttpTransport``.  When ``renderer``
    is supplied, it renders each page through the browser gateway instead; the
    browser still has no direct egress.  All discovery remains constrained by
    one crawl policy and one breadth-first frontier.
    """

    resolved_policy = policy or CrawlPolicy()
    resolved_transport = transport or SafeHttpTransport()
    request_ledger = _CrawlRequestLedger(resolved_policy, clock, sleep)
    robots_gate = _RobotsGate(resolved_transport, resolved_policy, request_ledger)

    frontier = CrawlFrontier(
        seed,
        resolved_policy,
        robots_checker=robots_gate.allows if resolved_policy.respect_robots else None,
    )
    pages: list[SitePageRecord] = []
    paths: list[Path] = []
    counts = {"written": 0, "duplicates": 0, "empty": 0, "failed": 0}
    fetched_pages = 0

    while (entry := frontier.pop()) is not None:
        remaining = request_ledger.remaining_bytes
        if remaining <= 0:
            frontier.mark_failed(entry.canonical_url, "crawl total byte budget exhausted")
            pages.append(
                SitePageRecord(
                    entry.canonical_url,
                    entry.depth,
                    "failed",
                    error="crawl total byte budget exhausted",
                )
            )
            counts["failed"] += 1
            break

        try:
            request_ledger.before_request()
            try:
                acquired = _acquire_page(
                    entry.canonical_url,
                    resolved_policy,
                    resolved_transport,
                    renderer,
                    browser_policy,
                    remaining,
                )
            except BrowserRenderFailure as exc:
                request_ledger.add_bytes(exc.network_bytes)
                raise
            fetched_pages += 1
            request_ledger.add_bytes(acquired.byte_size)
            if not _content_type_allowed(
                acquired.content_type, resolved_policy.allowed_content_types
            ):
                raise ValueError(
                    f"crawl response content type is not allowed: {acquired.content_type or 'unknown'}"
                )

            extraction = (
                _extract_html(acquired.html, acquired.final_url)
                if acquired.content_type != "text/markdown"
                else _HtmlExtraction("", acquired.final_url, ())
            )
            canonical_url = _safe_canonical(extraction.canonical_url, acquired.final_url)
            body = (
                acquired.html.strip()
                if acquired.content_type == "text/markdown"
                else _extract_body(acquired.html)
            )
            outcome = write_corpus_item_result(
                CorpusItem(
                    platform="web",
                    source_url=acquired.final_url,
                    canonical_url=canonical_url,
                    title=extraction.title or canonical_url,
                    body=body,
                    source_profile=acquired.source_profile,
                    extra={
                        "depth": entry.depth,
                        "status_code": acquired.status_code,
                        "content_type": acquired.content_type,
                        "byte_size": acquired.byte_size,
                    },
                ),
                out_dir,
                scraped_at=scraped_at,
            )
            counts[outcome.outcome + ("s" if outcome.outcome == "duplicate" else "")] += 1
            if outcome.path is not None and outcome.path not in paths:
                paths.append(outcome.path)
            discovered = frontier.discover(
                extraction.links,
                parent_url=acquired.final_url,
                depth=entry.depth + 1,
            )
            frontier.mark_done(entry.canonical_url)
            pages.append(
                SitePageRecord(
                    url=canonical_url,
                    depth=entry.depth,
                    outcome=outcome.outcome,
                    status_code=acquired.status_code,
                    content_type=acquired.content_type,
                    byte_size=acquired.byte_size,
                    content_hash=hashlib.sha256(body.strip().encode("utf-8")).hexdigest(),
                    title=extraction.title,
                    path=outcome.path,
                    links_discovered=len(discovered),
                    source_profile=acquired.source_profile,
                )
            )
        except Exception as exc:  # noqa: BLE001 - every attempted page needs an exact outcome
            safe_error = redact_sensitive_text(f"{type(exc).__name__}: {exc}")[:1_000]
            frontier.mark_failed(entry.canonical_url, safe_error)
            counts["failed"] += 1
            pages.append(
                SitePageRecord(
                    entry.canonical_url,
                    entry.depth,
                    "failed",
                    error=safe_error,
                    source_profile="browser" if renderer else "static",
                )
            )

    for unavailable in (
        item
        for item in frontier.entries
        if item.skip_reason is FrontierSkipReason.ROBOTS_UNAVAILABLE
    ):
        counts["failed"] += 1
        pages.append(
            SitePageRecord(
                unavailable.canonical_url,
                unavailable.depth,
                "failed",
                error="robots.txt unavailable; crawl failed closed",
                source_profile="browser" if renderer else "static",
            )
        )

    status = "complete" if counts["failed"] == 0 else ("partial" if paths else "failed")
    return SiteCollectionResult(
        seed=frontier.seed,
        policy=resolved_policy,
        status=status,
        written=counts["written"],
        duplicates=counts["duplicates"],
        empty=counts["empty"],
        failed=counts["failed"],
        fetched_pages=fetched_pages,
        total_bytes=request_ledger.total_bytes,
        paths=tuple(paths),
        pages=tuple(pages),
        frontier_events=frontier.events,
        frontier_exhausted=frontier.exhausted,
        page_limit_reached=frontier.page_limit_reached,
    )


def write_site_receipt(
    result: SiteCollectionResult,
    out_dir: str | Path,
    *,
    created_at: str | None = None,
) -> Path:
    """Persist a portable, uniquely identified receipt for one site collection."""

    root = Path(out_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    provenance = root / "_provenance"
    runs = provenance / "site-runs"
    for directory in (provenance, runs):
        if directory.is_symlink():
            raise ValueError("site receipt directory must not be a symlink")
        directory.mkdir(exist_ok=True)
        if not directory.resolve().is_relative_to(root):
            raise ValueError("site receipt directory escapes the output root")
    timestamp, filename_timestamp = _normalize_receipt_timestamp(
        created_at or datetime.now(timezone.utc).isoformat()
    )
    run_id = uuid.uuid4().hex[:16]
    pages = []
    for page in result.pages:
        row = asdict(page)
        row["path"] = _relative_result_path(page.path, root)
        row["record_hash"] = _record_hash(page.path, root)
        pages.append(row)
    events = []
    for event in result.frontier_events:
        row = asdict(event)
        row["status"] = event.status.value
        row["skip_reason"] = event.skip_reason.value if event.skip_reason else None
        events.append(row)
    payload = {
        "contract": "provenance-site-acquisition.v1",
        "run_id": run_id,
        "created_at": timestamp,
        "seed": result.seed,
        "policy": asdict(result.policy),
        "status": result.status,
        "counts": {
            "written": result.written,
            "duplicates": result.duplicates,
            "empty": result.empty,
            "failed": result.failed,
            "fetched_pages": result.fetched_pages,
            "total_bytes": result.total_bytes,
        },
        "paths": [_relative_result_path(path, root) for path in result.paths],
        "pages": pages,
        "frontier_events": events,
        "frontier_exhausted": result.frontier_exhausted,
        "page_limit_reached": result.page_limit_reached,
    }
    filename = f"{filename_timestamp}-{run_id}.json"
    destination = runs / filename
    if destination.resolve().parent != runs.resolve():
        raise ValueError("site receipt destination escapes its run directory")
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=runs, prefix=".site-receipt-", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def verify_site_receipt(
    receipt_path: str | Path,
    out_dir: str | Path,
    *,
    max_receipt_bytes: int = 10 * 1024 * 1024,
) -> SiteReceiptVerification:
    """Verify one receipt and the hashes of every referenced corpus record."""

    errors: list[str] = []
    checked = 0
    try:
        if max_receipt_bytes <= 0:
            raise ValueError("max_receipt_bytes must be positive")
        root = Path(out_dir).expanduser().resolve()
        runs = (root / "_provenance" / "site-runs").resolve()
        receipt = Path(receipt_path).expanduser()
        resolved_receipt = receipt.resolve()
        if receipt.is_symlink() or not resolved_receipt.is_relative_to(runs):
            raise ValueError("site receipt path escapes its run directory")
        if not resolved_receipt.is_file():
            raise ValueError("site receipt must be a regular file")
        if resolved_receipt.stat().st_size > max_receipt_bytes:
            raise ValueError("site receipt exceeds the verification byte limit")
        payload = json.loads(resolved_receipt.read_text(encoding="utf-8"))
        validation_errors = _validate_site_receipt_payload(payload, resolved_receipt)
        errors.extend(validation_errors)
        pages = payload.get("pages", []) if isinstance(payload, dict) else []
        for index, row in enumerate(pages):
            if not isinstance(row, dict):
                continue
            relative = row.get("path")
            if relative is None:
                continue
            expected = row.get("record_hash")
            if not isinstance(relative, str) or not isinstance(expected, str):
                errors.append(f"page {index}: path and record_hash must be strings")
                continue
            try:
                candidate = _receipt_record_path(relative, root)
                actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
                checked += 1
                if actual != expected:
                    errors.append(f"page {index}: record hash mismatch")
            except (OSError, ValueError) as exc:
                errors.append(f"page {index}: {redact_sensitive_text(str(exc))[:300]}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(redact_sensitive_text(str(exc))[:300])
    return SiteReceiptVerification(not errors, checked, tuple(errors))


def _validate_site_receipt_payload(payload: object, receipt: Path) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["site receipt must be a JSON object"]
    expected_top = {
        "contract",
        "run_id",
        "created_at",
        "seed",
        "policy",
        "status",
        "counts",
        "paths",
        "pages",
        "frontier_events",
        "frontier_exhausted",
        "page_limit_reached",
    }
    if set(payload) != expected_top:
        errors.append("site receipt fields do not match the v1 contract")
    if payload.get("contract") != "provenance-site-acquisition.v1":
        errors.append("site receipt contract is missing or unsupported")

    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or re.fullmatch(r"[a-f0-9]{16}", run_id) is None:
        errors.append("site receipt run_id is invalid")
    elif not receipt.name.endswith(f"-{run_id}.json"):
        errors.append("site receipt filename does not match run_id")
    created_at = payload.get("created_at")
    if not isinstance(created_at, str):
        errors.append("site receipt created_at must be a string")
    else:
        try:
            _normalize_receipt_timestamp(created_at)
        except ValueError as exc:
            errors.append(str(exc))

    policy = payload.get("policy")
    policy_fields = {item.name for item in fields(CrawlPolicy)}
    normalized_policy: CrawlPolicy | None = None
    if not isinstance(policy, dict) or set(policy) != policy_fields:
        errors.append("site receipt policy does not match the v1 contract")
    else:
        try:
            tuple_fields = {
                "allowed_origins",
                "allow_patterns",
                "deny_patterns",
                "strip_query_params",
                "allowed_content_types",
            }
            if any(
                not isinstance(policy[key], list)
                or any(not isinstance(item, str) for item in policy[key])
                for key in tuple_fields
            ):
                raise ValueError("invalid policy string list")
            for key in (
                "max_pages",
                "max_depth",
                "max_frontier_entries",
                "max_links_per_page",
                "max_page_bytes",
                "max_total_bytes",
                "max_redirects",
            ):
                if not _is_nonnegative_int(policy[key]):
                    raise ValueError("invalid policy integer")
            for key in ("same_origin", "respect_robots", "robots_fail_closed"):
                if not isinstance(policy[key], bool):
                    raise ValueError("invalid policy boolean")
            for key in ("min_interval", "timeout_seconds"):
                if (
                    not isinstance(policy[key], (int, float))
                    or isinstance(policy[key], bool)
                    or not math.isfinite(policy[key])
                ):
                    raise ValueError("invalid policy number")
            for origin in policy["allowed_origins"]:
                _normalize_origin(origin)
            normalized_policy = CrawlPolicy(
                **{
                    key: tuple(value) if key in tuple_fields and isinstance(value, list) else value
                    for key, value in policy.items()
                }
            )
        except (TypeError, ValueError):
            errors.append("site receipt policy values are invalid")

    seed = payload.get("seed")
    if not isinstance(seed, str):
        errors.append("site receipt seed must be a string")
    else:
        try:
            canonical_seed = canonicalize_url(
                seed,
                strip_query_params=(
                    normalized_policy.strip_query_params
                    if normalized_policy is not None
                    else DEFAULT_TRACKING_QUERY_KEYS
                ),
            )
            if canonical_seed != seed:
                errors.append("site receipt seed is not canonical")
        except (UnsafeUrlError, ValueError):
            errors.append("site receipt seed is invalid")

    counts = payload.get("counts")
    expected_counts = {
        "written",
        "duplicates",
        "empty",
        "failed",
        "fetched_pages",
        "total_bytes",
    }
    if not isinstance(counts, dict) or set(counts) != expected_counts:
        errors.append("site receipt counts do not match the v1 contract")
        counts = None
    elif any(not _is_nonnegative_int(value) for value in counts.values()):
        errors.append("site receipt counts must be non-negative integers")
        counts = None

    paths = payload.get("paths")
    if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
        errors.append("site receipt paths must be a list of strings")
        paths = []
    elif len(paths) != len(set(paths)):
        errors.append("site receipt paths must be unique")

    pages = payload.get("pages")
    page_fields = {item.name for item in fields(SitePageRecord)} | {"record_hash"}
    outcomes = {"written": 0, "duplicate": 0, "empty": 0, "failed": 0}
    referenced_paths: list[str] = []
    if not isinstance(pages, list):
        errors.append("site receipt pages must be a list")
        pages = []
    for index, row in enumerate(pages):
        prefix = f"page {index}"
        if not isinstance(row, dict):
            errors.append(f"{prefix}: row must be an object")
            continue
        if set(row) != page_fields:
            errors.append(f"{prefix}: fields do not match the v1 contract")
            continue
        outcome = row.get("outcome")
        if outcome not in outcomes:
            errors.append(f"{prefix}: outcome is invalid")
        else:
            outcomes[outcome] += 1
        if not isinstance(row.get("url"), str):
            errors.append(f"{prefix}: url must be a string")
        else:
            try:
                assert_safe_public_url(row["url"])
            except (UnsafeUrlError, ValueError):
                errors.append(f"{prefix}: url is invalid")
        for key in ("depth", "status_code", "byte_size", "links_discovered"):
            if not _is_nonnegative_int(row.get(key)):
                errors.append(f"{prefix}: {key} must be a non-negative integer")
        for key in ("content_type", "content_hash", "title", "source_profile", "error"):
            if not isinstance(row.get(key), str):
                errors.append(f"{prefix}: {key} must be a string")
        content_hash = row.get("content_hash")
        if content_hash and (
            not isinstance(content_hash, str) or re.fullmatch(r"[a-f0-9]{64}", content_hash) is None
        ):
            errors.append(f"{prefix}: content_hash is invalid")
        relative = row.get("path")
        record_hash = row.get("record_hash")
        if relative is None:
            if record_hash is not None:
                errors.append(f"{prefix}: record_hash requires a path")
        elif not isinstance(relative, str):
            errors.append(f"{prefix}: path must be a string or null")
        else:
            referenced_paths.append(relative)
            if (
                not isinstance(record_hash, str)
                or re.fullmatch(r"[a-f0-9]{64}", record_hash) is None
            ):
                errors.append(f"{prefix}: record_hash is invalid")

    expected_paths = list(dict.fromkeys(referenced_paths))
    if paths != expected_paths:
        errors.append("site receipt paths do not match page record paths")
    if counts is not None:
        if counts["written"] != outcomes["written"]:
            errors.append("site receipt written count does not match pages")
        if counts["duplicates"] != outcomes["duplicate"]:
            errors.append("site receipt duplicate count does not match pages")
        if counts["empty"] != outcomes["empty"]:
            errors.append("site receipt empty count does not match pages")
        if counts["failed"] != outcomes["failed"]:
            errors.append("site receipt failed count does not match pages")
        if counts["fetched_pages"] > len(pages):
            errors.append("site receipt fetched_pages exceeds page outcomes")

    status = payload.get("status")
    expected_status = (
        "complete" if outcomes["failed"] == 0 else ("partial" if expected_paths else "failed")
    )
    if status not in {"complete", "partial", "failed"} or status != expected_status:
        errors.append("site receipt status does not match page outcomes")

    events = payload.get("frontier_events")
    event_fields = {item.name for item in fields(FrontierEntry)}
    unavailable_urls: set[str] = set()
    latest_frontier_status: dict[str, str] = {}
    if not isinstance(events, list) or not events:
        errors.append("site receipt frontier_events must be a non-empty list")
        events = []
    for index, row in enumerate(events):
        prefix = f"frontier event {index}"
        if not isinstance(row, dict) or set(row) != event_fields:
            errors.append(f"{prefix}: fields do not match the v1 contract")
            continue
        if row.get("status") not in {item.value for item in FrontierStatus}:
            errors.append(f"{prefix}: status is invalid")
        skip_reason = row.get("skip_reason")
        if skip_reason is not None and skip_reason not in {
            item.value for item in FrontierSkipReason
        }:
            errors.append(f"{prefix}: skip_reason is invalid")
        if skip_reason == FrontierSkipReason.ROBOTS_UNAVAILABLE.value and isinstance(
            row.get("canonical_url"), str
        ):
            unavailable_urls.add(row["canonical_url"])
        if (row.get("status") == FrontierStatus.SKIPPED.value) != (skip_reason is not None):
            errors.append(f"{prefix}: skipped status and skip_reason are inconsistent")
        if not _is_nonnegative_int(row.get("depth")):
            errors.append(f"{prefix}: depth must be a non-negative integer")
        for key in ("canonical_url", "error"):
            if not isinstance(row.get(key), str):
                errors.append(f"{prefix}: {key} must be a string")
        if row.get("parent_url") is not None and not isinstance(row.get("parent_url"), str):
            errors.append(f"{prefix}: parent_url must be a string or null")
        for key in ("canonical_url", "parent_url"):
            value = row.get(key)
            if isinstance(value, str):
                try:
                    assert_safe_public_url(value)
                except (UnsafeUrlError, ValueError):
                    errors.append(f"{prefix}: {key} is invalid")
        if isinstance(row.get("canonical_url"), str) and isinstance(row.get("status"), str):
            latest_frontier_status[row["canonical_url"]] = row["status"]

    failed_urls = {
        row.get("url") for row in pages if isinstance(row, dict) and row.get("outcome") == "failed"
    }
    if not unavailable_urls.issubset(failed_urls):
        errors.append("robots-unavailable events require failed page outcomes")
    exhausted = payload.get("frontier_exhausted")
    page_limit = payload.get("page_limit_reached")
    if not isinstance(exhausted, bool) or not isinstance(page_limit, bool):
        errors.append("site receipt frontier flags must be booleans")
    elif exhausted and page_limit:
        errors.append("site receipt cannot be exhausted and page-limited")
    else:
        queued = any(
            status in {FrontierStatus.QUEUED.value, FrontierStatus.IN_PROGRESS.value}
            for status in latest_frontier_status.values()
        )
        if exhausted and queued:
            errors.append("site receipt exhausted frontier still has pending entries")
        if page_limit and not queued:
            errors.append("site receipt page limit requires a pending frontier entry")
    return errors


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _normalize_receipt_timestamp(value: str) -> tuple[str, str]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("created_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("created_at must include a timezone")
    normalized = parsed.isoformat()
    filename = parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return normalized, filename


def _relative_result_path(path: Path | None, root: Path) -> str | None:
    if path is None:
        return None
    candidate = path.expanduser().resolve()
    if path.is_symlink() or not candidate.is_relative_to(root):
        raise ValueError("site result path escapes the output root")
    return candidate.relative_to(root).as_posix()


def _record_hash(path: Path | None, root: Path) -> str | None:
    if path is None:
        return None
    relative = _relative_result_path(path, root)
    if relative is None:  # pragma: no cover - guarded above
        return None
    candidate = _receipt_record_path(relative, root)
    return hashlib.sha256(candidate.read_bytes()).hexdigest()


def _receipt_record_path(relative: str, root: Path) -> Path:
    candidate_path = Path(relative)
    if candidate_path.is_absolute() or ".." in candidate_path.parts:
        raise ValueError("site record path is not a confined relative path")
    unresolved = root / candidate_path
    candidate = unresolved.resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("site record path escapes the output root")
    if unresolved.is_symlink() or not candidate.is_file():
        raise ValueError("site record path must name a regular non-symlink file")
    return candidate


@dataclass(frozen=True)
class _AcquiredPage:
    final_url: str
    status_code: int
    content_type: str
    html: str
    byte_size: int
    source_profile: str


def _acquire_page(
    url: str,
    policy: CrawlPolicy,
    transport: SafeHttpTransport,
    renderer: BrowserDriver | None,
    browser_policy: BrowserPolicy | None,
    remaining_bytes: int,
) -> _AcquiredPage:
    page_limit = min(policy.max_page_bytes, remaining_bytes)
    if renderer is None:
        response = transport.get(
            url,
            timeout=policy.timeout_seconds,
            max_bytes=page_limit,
            max_redirects=policy.max_redirects,
        )
        response.raise_for_status()
        return _AcquiredPage(
            final_url=assert_safe_public_url(response.url),
            status_code=response.status_code,
            content_type=response.media_type,
            html=response.text,
            byte_size=len(response.body),
            source_profile="static",
        )

    requested_browser_policy = browser_policy or BrowserPolicy()
    bounded_total = min(requested_browser_policy.max_total_bytes, page_limit)
    bounded_resource = min(requested_browser_policy.max_resource_bytes, bounded_total)
    bounded_policy = replace(
        requested_browser_policy,
        timeout_seconds=min(requested_browser_policy.timeout_seconds, policy.timeout_seconds),
        max_resource_bytes=bounded_resource,
        max_total_bytes=bounded_total,
    )
    rendered = render_page(
        url,
        driver=renderer,
        policy=bounded_policy,
        transport=transport,
    )
    rendered_bytes = len(rendered.html.encode("utf-8"))
    byte_size = max(rendered.network_bytes, rendered_bytes)
    if byte_size > page_limit:
        raise ValueError(f"rendered page exceeded {page_limit} bytes")
    return _AcquiredPage(
        final_url=rendered.final_url,
        status_code=rendered.status_code,
        content_type=rendered.content_type.partition(";")[0].strip().lower(),
        html=rendered.html,
        byte_size=byte_size,
        source_profile="browser",
    )


@dataclass(frozen=True)
class _HtmlExtraction:
    title: str
    canonical_url: str
    links: tuple[str, ...]


class _DocumentParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[str] = []
        self.canonical_url = ""
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs}
        if tag.lower() == "title":
            self._in_title = True
        if tag.lower() == "base" and values.get("href"):
            self.base_url = urljoin(self.base_url, values["href"] or "")
        if tag.lower() == "a" and values.get("href"):
            self.links.append(urljoin(self.base_url, values["href"] or ""))
        if tag.lower() == "link" and values.get("href"):
            rel = (values.get("rel") or "").lower().split()
            if "canonical" in rel and not self.canonical_url:
                self.canonical_url = urljoin(self.base_url, values["href"] or "")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


def _extract_html(html: str, base_url: str) -> _HtmlExtraction:
    parser = _DocumentParser(base_url)
    parser.feed(html)
    return _HtmlExtraction(
        title=" ".join(" ".join(parser.title_parts).split()),
        canonical_url=parser.canonical_url or base_url,
        links=tuple(parser.links),
    )


def _extract_body(html: str) -> str:
    extracted = trafilatura.extract(
        html,
        include_links=True,
        include_images=False,
        output_format="markdown",
    )
    return (extracted or html_to_text(html)).strip()


def _safe_canonical(candidate: str, base_url: str) -> str:
    try:
        return canonicalize_url(candidate, base_url=base_url)
    except (UnsafeUrlError, ValueError):
        return canonicalize_url(base_url)


def _content_type_allowed(value: str, allowed: tuple[str, ...]) -> bool:
    normalized = value.partition(";")[0].strip().lower()
    for pattern in allowed:
        candidate = pattern.partition(";")[0].strip().lower()
        if candidate == "*/*" or candidate == normalized:
            return True
        if candidate.endswith("/*") and normalized.startswith(candidate[:-1]):
            return True
    return False


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    port = parsed.port
    default = (parsed.scheme == "http" and port in {None, 80}) or (
        parsed.scheme == "https" and port in {None, 443}
    )
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme.lower()}://{host.lower()}{'' if default else f':{port}'}"


def _normalize_origin(origin: str) -> str:
    canonical = canonicalize_url(origin)
    parsed = urlsplit(canonical)
    if parsed.path not in {"", "/"} or parsed.query:
        raise ValueError("allowed origins must not include a path or query")
    return _origin(canonical)


def _matches_any(url: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(url.lower(), pattern.lower()) for pattern in patterns)
