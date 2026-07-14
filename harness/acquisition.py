"""Deterministic acquisition receipts and opt-in resume semantics."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import BaseScraper, _scraped_at_timestamp, write_corpus_item_result
from .url_safety import (
    UnsafeUrlError,
    inspect_url_credentials,
    redact_sensitive_text,
    sanitize_metadata,
    sanitize_url_for_persistence,
)

CONTRACT_VERSION = "provenance-acquisition.v1"


@dataclass(frozen=True)
class CollectionSpec:
    contract: str
    adapter: str
    adapter_class: str
    target: str
    target_identity_hash: str
    limit: int
    options: dict[str, object]

    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CollectionResult:
    run_id: str
    fingerprint: str
    status: str
    written: int
    duplicates: int
    empty: int
    failed: int
    paths: tuple[Path, ...]
    receipt_path: Path
    error: str = ""


def collect(
    scraper: BaseScraper,
    target: str,
    out_dir: str | Path,
    *,
    limit: int = 25,
    resume: bool = False,
    refresh: bool = False,
    scraped_at: str | None = None,
) -> CollectionResult:
    """Run an adapter with an atomic, non-secret acquisition receipt.

    Existing adapter and writer APIs remain unchanged. Resume is explicit and
    only reuses a matching, complete receipt whose referenced files still exist.
    """

    if limit <= 0:
        raise ValueError("limit must be positive")
    if resume and refresh:
        raise ValueError("resume and refresh are mutually exclusive")

    root = Path(out_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    safe_target = _sanitize_target(target)
    raw_options = scraper.acquisition_options()
    safe_options = sanitize_metadata(raw_options)
    if not isinstance(raw_options, dict) or safe_options != raw_options:
        raise ValueError("acquisition options must be a non-secret mapping")
    spec = CollectionSpec(
        contract=CONTRACT_VERSION,
        adapter=str(scraper.platform),
        adapter_class=f"{type(scraper).__module__}.{type(scraper).__qualname__}",
        target=safe_target,
        target_identity_hash=hashlib.sha256(target.encode("utf-8")).hexdigest(),
        limit=limit,
        options=safe_options,
    )
    fingerprint = spec.fingerprint()
    receipt_dir = _safe_receipt_dir(root)
    fingerprint_dir = _safe_fingerprint_dir(receipt_dir, fingerprint)
    latest_path = fingerprint_dir / "latest.json"
    latest_complete_path = fingerprint_dir / "latest-complete.json"

    if resume and latest_complete_path.exists():
        reused = _load_reusable_result(latest_complete_path, root, fingerprint)
        if reused is not None:
            return reused

    timestamp = _scraped_at_timestamp(scraped_at)
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = uuid.uuid4().hex[:16]
    receipt_path = (
        fingerprint_dir / f"{started_at.replace(':', '').replace('+', '_')}-{run_id}.json"
    )
    paths: list[Path] = []
    records: list[dict[str, str]] = []
    counts = {"written": 0, "duplicates": 0, "empty": 0, "failed": 0}
    _write_receipt(
        receipt_path,
        _receipt_payload(spec, fingerprint, run_id, "in_progress", started_at, counts, [], [], ""),
    )

    error = ""
    try:
        for item in scraper.scrape(target, limit=limit):
            outcome = write_corpus_item_result(item, root, scraped_at=timestamp)
            if outcome.outcome == "written" and outcome.path is not None:
                paths.append(outcome.path)
                records.append(
                    {
                        "path": outcome.path.relative_to(root).as_posix(),
                        "content_hash": outcome.content_hash,
                        "record_hash": hashlib.sha256(outcome.path.read_bytes()).hexdigest(),
                    }
                )
                counts["written"] += 1
            elif outcome.outcome == "duplicate":
                counts["duplicates"] += 1
                if outcome.path is not None and outcome.path not in paths:
                    paths.append(outcome.path)
                    records.append(
                        {
                            "path": outcome.path.relative_to(root).as_posix(),
                            "content_hash": outcome.content_hash,
                            "record_hash": hashlib.sha256(outcome.path.read_bytes()).hexdigest(),
                        }
                    )
            elif outcome.outcome == "empty":
                counts["empty"] += 1
    except Exception as exc:  # noqa: BLE001 - the receipt must survive partial runs
        counts["failed"] += 1
        error = _redact_error(f"{type(exc).__name__}: {exc}", root)

    status = "complete" if not error else ("partial" if paths else "failed")
    relative_paths = [path.relative_to(root).as_posix() for path in paths]
    final_payload = _receipt_payload(
        spec,
        fingerprint,
        run_id,
        status,
        started_at,
        counts,
        relative_paths,
        records,
        error,
    )
    _write_receipt(receipt_path, final_payload)
    _write_receipt(latest_path, final_payload)
    if status == "complete":
        _write_receipt(latest_complete_path, final_payload)
    return CollectionResult(
        run_id=run_id,
        fingerprint=fingerprint,
        status=status,
        written=counts["written"],
        duplicates=counts["duplicates"],
        empty=counts["empty"],
        failed=counts["failed"],
        paths=tuple(paths),
        receipt_path=receipt_path,
        error=error,
    )


def _sanitize_target(target: str) -> str:
    parts = [part.strip() for part in target.split(",")]
    safe_parts: list[str] = []
    for part in parts:
        if Path(part).is_absolute():
            safe_parts.append("<local-path>")
            continue
        if "://" in part:
            credentials = inspect_url_credentials(part)
            if credentials.has_userinfo or credentials.sensitive_query_keys:
                raise UnsafeUrlError("acquisition target URL must not contain credentials")
            part = sanitize_url_for_persistence(part)
        safe_parts.append(redact_sensitive_text(part))
    return ",".join(safe_parts)


def _safe_receipt_dir(root: Path) -> Path:
    provenance = root / "_provenance"
    runs = provenance / "runs"
    for candidate in (provenance, runs):
        if candidate.is_symlink():
            raise ValueError("provenance receipt directory must not be a symlink")
        candidate.mkdir(exist_ok=True)
        if candidate.resolve().parent not in {root, provenance.resolve()}:
            raise ValueError("provenance receipt directory escapes the output root")
    return runs


def _safe_fingerprint_dir(receipt_dir: Path, fingerprint: str) -> Path:
    target = receipt_dir / fingerprint
    if target.is_symlink():
        raise ValueError("fingerprint receipt directory must not be a symlink")
    target.mkdir(exist_ok=True)
    if target.resolve().parent != receipt_dir.resolve():
        raise ValueError("fingerprint receipt directory escapes the receipt root")
    return target


def _receipt_payload(
    spec: CollectionSpec,
    fingerprint: str,
    run_id: str,
    status: str,
    started_at: str,
    counts: dict[str, int],
    paths: list[str],
    records: list[dict[str, str]],
    error: str,
) -> dict[str, Any]:
    return {
        "contract": CONTRACT_VERSION,
        "run_id": run_id,
        "fingerprint": fingerprint,
        "status": status,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat() if status != "in_progress" else None,
        "spec": asdict(spec),
        "counts": dict(counts),
        "paths": paths,
        "records": records,
        "error": error or None,
    }


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".receipt-", delete=False
    ) as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_reusable_result(
    receipt_path: Path, root: Path, fingerprint: str
) -> CollectionResult | None:
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        if payload.get("fingerprint") != fingerprint or payload.get("status") != "complete":
            return None
        raw_paths = payload.get("paths", [])
        raw_records = payload.get("records", [])
        if not isinstance(raw_paths, list) or not isinstance(raw_records, list):
            return None
        expected_records = {
            record.get("path"): record
            for record in raw_records
            if isinstance(record, dict) and isinstance(record.get("path"), str)
        }
        if len(expected_records) != len(raw_paths):
            return None
        paths: list[Path] = []
        for raw in raw_paths:
            if not isinstance(raw, str):
                return None
            unresolved = root / raw
            if unresolved.is_symlink():
                return None
            candidate = unresolved.resolve()
            if not candidate.is_relative_to(root) or not candidate.is_file():
                return None
            record = expected_records.get(raw)
            if (
                record is None
                or record.get("record_hash") != hashlib.sha256(candidate.read_bytes()).hexdigest()
            ):
                return None
            paths.append(candidate)
        counts = payload.get("counts", {})
        return CollectionResult(
            run_id=str(payload.get("run_id", "")),
            fingerprint=fingerprint,
            status="reused",
            written=int(counts.get("written", 0)),
            duplicates=int(counts.get("duplicates", 0)),
            empty=int(counts.get("empty", 0)),
            failed=0,
            paths=tuple(paths),
            receipt_path=receipt_path,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _redact_error(message: str, root: Path) -> str:
    redacted = redact_sensitive_text(message)
    redacted = redacted.replace(str(root), "<output-root>")
    redacted = redacted.replace(str(Path.home()), "<home>")
    return re.sub(r"/(?:Users|home)/[^\s:'\"]+", "<local-path>", redacted)
