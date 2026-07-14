"""Content-addressed, inert storage for operator-authorized binary assets."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .rights import SourcePolicy
from .url_safety import sanitize_metadata, sanitize_url_for_persistence


@dataclass(frozen=True)
class StoredAsset:
    asset_id: str
    blob_id: str
    relative_path: str
    sha256: str
    media_type: str
    byte_size: int
    source_url: str
    role: str
    alt: str
    source_policy: SourcePolicy
    source_id: str = ""
    item_id: str = ""

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["source_policy"] = self.source_policy.to_dict()
        return result


class AssetStore:
    """Store exact bytes below a hash-only path without executing or rendering them."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_asset_bytes: int = 25 * 1024 * 1024,
        max_total_bytes: int = 250 * 1024 * 1024,
        max_assets: int = 250,
    ) -> None:
        if min(max_asset_bytes, max_total_bytes, max_assets) <= 0:
            raise ValueError("asset limits must be positive")
        unresolved_root = Path(root).expanduser()
        if unresolved_root.is_symlink():
            raise ValueError("asset storage root must not be a symlink")
        self.root = unresolved_root.resolve()
        self.max_asset_bytes = max_asset_bytes
        self.max_total_bytes = max_total_bytes
        self.max_assets = max_assets
        self._stored_bytes = 0
        self._stored_count = 0

    def put(
        self,
        body: bytes,
        *,
        source_url: str,
        media_type: str,
        source_policy: SourcePolicy,
        role: str = "source",
        alt: str = "",
        expected_sha256: str = "",
        expected_size: int | None = None,
        source_id: str = "",
        item_id: str = "",
    ) -> StoredAsset:
        if not isinstance(body, bytes):
            raise TypeError("asset body must be bytes")
        size = len(body)
        if size > self.max_asset_bytes:
            raise ValueError(f"asset exceeds {self.max_asset_bytes} bytes")
        if self._stored_bytes + size > self.max_total_bytes:
            raise ValueError(f"assets exceed {self.max_total_bytes} total bytes")
        if self._stored_count >= self.max_assets:
            raise ValueError(f"assets exceed count limit {self.max_assets}")
        digest = hashlib.sha256(body).hexdigest()
        if expected_sha256 and expected_sha256 != digest:
            raise ValueError("asset sha256 does not match its declaration")
        if expected_size is not None and expected_size != size:
            raise ValueError("asset byte_size does not match its declaration")
        safe_media_type = _safe_short_text(media_type, "media_type")
        safe_role = _safe_short_text(role, "role")
        safe_alt = str(sanitize_metadata(alt))
        safe_url = sanitize_url_for_persistence(source_url) if source_url else ""
        safe_source_id = _safe_identity(source_id, "source_id")
        safe_item_id = _safe_identity(item_id, "item_id")
        relative = canonical_blob_path(digest)
        target = self._contained_target(relative)
        _publish_bytes(target, body)
        self._stored_count += 1
        self._stored_bytes += size
        return StoredAsset(
            asset_id=asset_id_for(
                digest,
                source_id=safe_source_id,
                item_id=safe_item_id,
                source_url=safe_url,
                role=safe_role,
            ),
            blob_id=f"blob-{digest}",
            relative_path=relative.as_posix(),
            sha256=digest,
            media_type=safe_media_type,
            byte_size=size,
            source_url=safe_url,
            role=safe_role,
            alt=safe_alt,
            source_policy=source_policy,
            source_id=safe_source_id,
            item_id=safe_item_id,
        )

    def verify(self, asset: StoredAsset) -> bool:
        try:
            path = self._contained_target(Path(asset.relative_path), create=False)
            if path.is_symlink() or not path.is_file():
                return False
            body = path.read_bytes()
            return (
                asset.relative_path == canonical_blob_path(asset.sha256).as_posix()
                and asset.blob_id == f"blob-{asset.sha256}"
                and asset.asset_id
                == asset_id_for(
                    asset.sha256,
                    source_id=asset.source_id,
                    item_id=asset.item_id,
                    source_url=asset.source_url,
                    role=asset.role,
                )
                and len(body) == asset.byte_size
                and hashlib.sha256(body).hexdigest() == asset.sha256
            )
        except (OSError, ValueError):
            return False

    def _contained_target(self, relative: Path, *, create: bool = True) -> Path:
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("asset path escapes output root")
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        base = self.root.resolve()
        target = base / relative
        for parent in [base, *target.parents]:
            if parent == base.parent:
                break
            if parent.exists() and parent.is_symlink():
                raise ValueError("asset storage path must not contain symlinks")
        if create:
            target.parent.mkdir(parents=True, exist_ok=True)
        if not target.resolve().is_relative_to(base):
            raise ValueError("asset path escapes output root")
        return target


def write_asset_manifest(path: str | Path, assets: Iterable[StoredAsset]) -> Path:
    destination = Path(path)
    if destination.is_symlink():
        raise ValueError("asset manifest path must not be a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {"contract": "provenance-assets.v1", "assets": [a.to_dict() for a in assets]}
    _atomic_text(destination, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return destination


def canonical_blob_path(digest: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("asset digest is invalid")
    return Path("_assets") / "sha256" / digest[:2] / digest


def asset_id_for(
    digest: str,
    *,
    source_id: str = "",
    item_id: str = "",
    source_url: str = "",
    role: str = "source",
) -> str:
    canonical_blob_path(digest)
    identity = f"{source_id}\0{item_id}" if source_id or item_id else f"{source_url}\0{role}"
    identity_hash = hashlib.sha256(f"{digest}\0{identity}".encode("utf-8")).hexdigest()
    return f"asset-{identity_hash}"


def merge_asset_manifest(path: str | Path, assets: Iterable[StoredAsset]) -> Path:
    """Atomically merge assets without silently losing an earlier run.

    A short-lived sibling lock directory makes concurrent writers fail closed
    instead of racing a read-modify-write cycle.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock = destination.with_name(f".{destination.name}.lock")
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise ValueError("asset manifest is currently being updated") from exc
    try:
        rows: dict[str, dict[str, object]] = {}
        if destination.exists():
            if destination.is_symlink():
                raise ValueError("asset manifest must not be a symlink")
            existing = json.loads(destination.read_text(encoding="utf-8"))
            if existing.get("contract") != "provenance-assets.v1" or not isinstance(
                existing.get("assets"), list
            ):
                raise ValueError("existing asset manifest is invalid")
            for row in existing["assets"]:
                if not isinstance(row, dict) or not isinstance(row.get("asset_id"), str):
                    raise ValueError("existing asset manifest contains an invalid row")
                rows[row["asset_id"]] = row
        for asset in assets:
            row = json.loads(json.dumps(asset.to_dict(), sort_keys=True))
            previous = rows.get(asset.asset_id)
            if previous is not None and previous != row:
                raise ValueError(f"asset manifest contains conflicting {asset.asset_id}")
            rows[asset.asset_id] = row
        payload = {
            "contract": "provenance-assets.v1",
            "assets": [rows[key] for key in sorted(rows)],
        }
        _atomic_text(destination, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return destination
    finally:
        lock.rmdir()


def _safe_short_text(value: str, label: str) -> str:
    clean = str(sanitize_metadata(value)).strip().lower()
    if not clean or len(clean) > 200 or "\n" in clean or "\r" in clean:
        raise ValueError(f"{label} must be a short, non-empty value")
    return clean


def _safe_identity(value: str, label: str) -> str:
    clean = str(sanitize_metadata(value)).strip()
    if len(clean) > 200 or "\n" in clean or "\r" in clean:
        raise ValueError(f"{label} must be a short value")
    return clean


def _publish_bytes(path: Path, body: bytes) -> None:
    if path.exists():
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != path.name:
            raise ValueError("existing content-addressed asset is invalid")
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".asset-", delete=False) as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != path.name:
                raise ValueError("concurrent asset publication produced invalid content")
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_text(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".manifest-", delete=False
    ) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
