"""Secret-free, rights-aware import of operator-provided source exports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping, cast
from urllib.parse import urlsplit

from .assets import (
    AssetStore,
    StoredAsset,
    asset_id_for,
    canonical_blob_path,
    write_asset_manifest,
)
from .rights import SourcePolicy
from .url_safety import (
    inspect_url_credentials,
    redact_sensitive_text,
    sanitize_metadata,
    sanitize_url_for_persistence,
)

PACKAGE_CONTRACT = "provenance-source-package.v1"
ASSET_CONTRACT = "provenance-assets.v1"
IMPORT_CONTRACT = "provenance-source-import.v1"
DEFAULT_MAX_ITEM_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_ITEMS = 1000
DEFAULT_MAX_MANIFEST_BYTES = 1024 * 1024
Modality = Literal["text", "document", "image", "audio", "video"]
Connector = Literal[
    "generic-export",
    "document-set",
    "media-set",
    "x-official-api-export",
    "x-account-export",
    "substack-export",
    "visual-board-export",
]

_CONNECTORS = {
    "generic-export",
    "document-set",
    "media-set",
    "x-official-api-export",
    "x-account-export",
    "substack-export",
    "visual-board-export",
}
_MODALITIES = {"text", "document", "image", "audio", "video"}
_PACKAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_SECRET_FIELD = re.compile(
    r"^(?:access[_-]?token|api[_-]?key|auth[_-]?header|bearer|client[_-]?secret|cookie|csrf|"
    r"jwt|oauth[_-]?token|password|private[_-]?key|refresh[_-]?token|secret|"
    r"session(?:[_-]?id)?|token)$",
    re.I,
)
_SESSION_PATH = re.compile(
    r"(?:^|[/\\])(?:cookies?|login data|local storage|session storage|web data|"
    r"browser profiles?|agentcookie)(?:[/\\]|$)",
    re.I,
)
_CREDENTIAL_VALUE = re.compile(
    r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+|"
    r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)


@dataclass(frozen=True)
class SourcePackageItem:
    item_id: str
    path: str
    modality: Modality
    mime: str
    title: str
    sha256: str
    byte_size: int
    source_url: str = ""
    transcript_path: str = ""
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.item_id.strip() or len(self.item_id) > 200:
            raise ValueError("source package item_id is invalid")
        _validate_relative_path(self.path)
        if self.transcript_path:
            _validate_relative_path(self.transcript_path)
        if self.modality not in _MODALITIES:
            raise ValueError(f"unsupported source package modality: {self.modality}")
        if not _DIGEST.fullmatch(self.sha256):
            raise ValueError("source package item sha256 is invalid")
        if self.byte_size < 0:
            raise ValueError("source package item byte_size must not be negative")
        if not self.mime.strip() or len(self.mime) > 200:
            raise ValueError("source package item mime is invalid")
        if not self.title.strip() or len(self.title) > 1000:
            raise ValueError("source package item title is invalid")
        if self.source_url:
            parsed = urlsplit(self.source_url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                raise ValueError("source package source_url must be an absolute HTTP(S) URL")
            inspection = inspect_url_credentials(self.source_url)
            if inspection.has_userinfo or inspection.sensitive_query_keys:
                raise ValueError("source package source_url contains credentials")
        assert_no_secrets(self.metadata)

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        if self.source_url:
            result["source_url"] = sanitize_url_for_persistence(self.source_url)
        return result


@dataclass(frozen=True)
class SourcePackageManifest:
    package_id: str
    connector: Connector
    created_at: str
    source_policy: SourcePolicy
    items: tuple[SourcePackageItem, ...]
    contract: str = PACKAGE_CONTRACT

    def __post_init__(self) -> None:
        if self.contract != PACKAGE_CONTRACT:
            raise ValueError(f"unsupported source package contract: {self.contract}")
        if not _PACKAGE_ID.fullmatch(self.package_id):
            raise ValueError("source package package_id is invalid")
        if self.connector not in _CONNECTORS:
            raise ValueError(f"unsupported source package connector: {self.connector}")
        _parse_timestamp(self.created_at)
        if not self.items:
            raise ValueError("source package must contain at least one item")
        ids = [item.item_id for item in self.items]
        paths = [item.path for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("source package item_id values must be unique")
        if len(paths) != len(set(paths)):
            raise ValueError("source package item paths must be unique")
        item_by_path = {item.path: item for item in self.items}
        for item in self.items:
            if item.transcript_path and item.transcript_path not in item_by_path:
                raise ValueError("source package transcript_path must reference a declared item")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract": self.contract,
            "package_id": self.package_id,
            "connector": self.connector,
            "created_at": self.created_at,
            "source_policy": self.source_policy.to_dict(),
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class ImportResult:
    output_dir: Path
    manifest_path: Path
    assets_path: Path
    receipt_path: Path
    assets: tuple[StoredAsset, ...]
    total_bytes: int


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    checked: int
    errors: tuple[str, ...]


def discover_source_package(
    package_dir: str | Path,
    *,
    package_id: str,
    connector: Connector,
    source_policy: SourcePolicy,
    metadata_path: str | Path | None = None,
    created_at: str | None = None,
    max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
) -> SourcePackageManifest:
    """Describe regular local export files without importing account/session state."""

    if min(max_item_bytes, max_total_bytes, max_items, max_manifest_bytes) <= 0:
        raise ValueError("source package discovery limits must be positive")
    unresolved_root = Path(package_dir).expanduser()
    if unresolved_root.is_symlink():
        raise ValueError("source package directory must not be a symlink")
    root = unresolved_root.resolve()
    if not root.is_dir():
        raise ValueError("source package directory must be a regular directory")
    unresolved_metadata = (
        Path(metadata_path).expanduser() if metadata_path else root / "source-metadata.jsonl"
    )
    if unresolved_metadata.is_symlink():
        raise ValueError("source package metadata must not be a symlink")
    metadata_file = unresolved_metadata.resolve()
    if not metadata_file.is_relative_to(root):
        raise ValueError("source package metadata must remain inside the package directory")
    metadata = _read_metadata(metadata_file, root) if metadata_file.exists() else {}
    items: list[SourcePackageItem] = []
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if _SESSION_PATH.search(relative):
            raise ValueError(f"source package contains browser/session state: {relative}")
        if path.is_symlink():
            raise ValueError(f"source package must not contain symlinks: {relative}")
        if not path.is_file() or path == metadata_file or path.name == "source-package.json":
            continue
        if path.name.startswith(".") or not _supported_extension(path.suffix):
            continue
        if _looks_like_source_manifest(path, max_manifest_bytes=max_manifest_bytes):
            continue
        if len(items) >= max_items:
            raise ValueError(f"source package exceeds item count limit {max_items}")
        overrides = metadata.get(relative, {})
        body = _read_file_bounded(path, max_item_bytes, label=f"source package item {relative}")
        total_bytes += len(body)
        if total_bytes > max_total_bytes:
            raise ValueError(f"source package exceeds {max_total_bytes} total bytes")
        mime = _string_override(overrides, "mime", _mime_from_path(path))
        modality = _string_override(overrides, "modality", _modality_from_mime(mime))
        _assert_text_payload_has_no_secrets(body, mime=mime, path=relative)
        source_url = _string_override(overrides, "source_url", "")
        transcript_path = _string_override(overrides, "transcript_path", "")
        raw_metadata = overrides.get("metadata", {})
        if not isinstance(raw_metadata, dict):
            raise ValueError(f"metadata for {relative} must be an object")
        item = SourcePackageItem(
            item_id=_string_override(
                overrides, "item_id", f"item-{hashlib.sha256(relative.encode()).hexdigest()[:16]}"
            ),
            path=relative,
            modality=cast(Modality, modality),
            mime=mime,
            title=_string_override(overrides, "title", path.stem),
            sha256=hashlib.sha256(body).hexdigest(),
            byte_size=len(body),
            source_url=source_url,
            transcript_path=transcript_path,
            metadata=cast(dict[str, object], sanitize_metadata(raw_metadata)),
        )
        items.append(item)
    timestamp = created_at or datetime.now(timezone.utc).isoformat()
    manifest = SourcePackageManifest(
        package_id=package_id,
        connector=connector,
        created_at=timestamp,
        source_policy=source_policy,
        items=tuple(items),
    )
    _assert_manifest_serialized_size(manifest, max_manifest_bytes)
    return manifest


def load_source_package(
    path: str | Path,
    *,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> SourcePackageManifest:
    if min(max_manifest_bytes, max_items) <= 0:
        raise ValueError("source package manifest limits must be positive")
    unresolved = Path(path).expanduser()
    if unresolved.is_symlink():
        raise ValueError("source package manifest must not be a symlink")
    manifest_path = unresolved.resolve()
    body = _read_file_bounded(manifest_path, max_manifest_bytes, label="source package manifest")
    raw = json.loads(body)
    assert_no_secrets(raw)
    if not isinstance(raw, dict):
        raise ValueError("source package manifest must be an object")
    source_policy = raw.get("source_policy")
    raw_items = raw.get("items")
    if not isinstance(source_policy, Mapping) or not isinstance(raw_items, list):
        raise ValueError("source package manifest is missing source_policy or items")
    if len(raw_items) > max_items:
        raise ValueError(f"source package exceeds item count limit {max_items}")
    items: list[SourcePackageItem] = []
    for value in raw_items:
        if not isinstance(value, dict):
            raise ValueError("source package items must be objects")
        items.append(_item_from_mapping(value))
    manifest = SourcePackageManifest(
        contract=_required_string(raw, "contract"),
        package_id=_required_string(raw, "package_id"),
        connector=cast(Connector, _required_string(raw, "connector")),
        created_at=_required_string(raw, "created_at"),
        source_policy=SourcePolicy.from_mapping(source_policy),
        items=tuple(items),
    )
    _assert_manifest_serialized_size(manifest, max_manifest_bytes)
    return manifest


def import_source_package(
    manifest_path: str | Path,
    output_dir: str | Path,
    *,
    max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> ImportResult:
    if min(max_item_bytes, max_total_bytes, max_items) <= 0:
        raise ValueError("source package import limits must be positive")
    unresolved_manifest = Path(manifest_path).expanduser()
    if unresolved_manifest.is_symlink():
        raise ValueError("source package manifest must not be a symlink")
    manifest_file = unresolved_manifest.resolve()
    package_root = manifest_file.parent
    manifest = load_source_package(manifest_file, max_items=max_items)
    if len(manifest.items) > max_items:
        raise ValueError(f"source package exceeds item count limit {max_items}")
    unresolved_destination = Path(output_dir).expanduser()
    if unresolved_destination.is_symlink():
        raise ValueError("source package output directory must not be a symlink")
    destination = unresolved_destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("source package output directory must be empty and not a symlink")
    declared_total = sum(item.byte_size for item in manifest.items)
    if any(item.byte_size > max_item_bytes for item in manifest.items):
        raise ValueError(f"source package item exceeds {max_item_bytes} bytes")
    if declared_total > max_total_bytes:
        raise ValueError(f"source package exceeds {max_total_bytes} total bytes")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-import-", dir=destination.parent))
    store = AssetStore(
        staging,
        max_asset_bytes=max_item_bytes,
        max_total_bytes=max_total_bytes,
        max_assets=max_items,
    )
    assets: list[StoredAsset] = []
    total = 0
    try:
        for item in manifest.items:
            source = _contained_regular_file(package_root, item.path)
            body = _read_file_bounded(
                source, max_item_bytes, label=f"source package item {item.item_id}"
            )
            _assert_text_payload_has_no_secrets(body, mime=item.mime, path=item.path)
            total += len(body)
            if total > max_total_bytes:
                raise ValueError(f"source package exceeds {max_total_bytes} total bytes")
            asset = store.put(
                body,
                source_url=item.source_url,
                media_type=item.mime,
                source_policy=manifest.source_policy,
                role=item.modality,
                alt=item.title,
                expected_sha256=item.sha256,
                expected_size=item.byte_size,
                source_id=manifest.package_id,
                item_id=item.item_id,
            )
            assets.append(asset)
        manifest_output = staging / "source-package.json"
        _atomic_json(manifest_output, manifest.to_dict())
        assets_output = write_asset_manifest(staging / "assets.json", assets)
        receipt = {
            "contract": IMPORT_CONTRACT,
            "package_id": manifest.package_id,
            "manifest_sha256": hashlib.sha256(manifest_output.read_bytes()).hexdigest(),
            "assets_sha256": hashlib.sha256(assets_output.read_bytes()).hexdigest(),
            "asset_count": len(assets),
            "total_bytes": total,
        }
        _atomic_json(staging / "import-receipt.json", receipt)
        if destination.exists():
            destination.rmdir()
        os.replace(staging, destination)
        return ImportResult(
            output_dir=destination,
            manifest_path=destination / "source-package.json",
            assets_path=destination / "assets.json",
            receipt_path=destination / "import-receipt.json",
            assets=tuple(assets),
            total_bytes=total,
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_import(output_dir: str | Path) -> VerificationResult:
    unresolved_root = Path(output_dir).expanduser()
    if unresolved_root.is_symlink():
        return VerificationResult(False, 0, ("import root must not be a symlink",))
    root = unresolved_root.resolve()
    errors: list[str] = []
    try:
        manifest_path = root / "source-package.json"
        assets_path = root / "assets.json"
        receipt_path = root / "import-receipt.json"
        manifest = load_source_package(manifest_path)
        manifest_bytes = _read_file_bounded(
            manifest_path, DEFAULT_MAX_MANIFEST_BYTES, label="source package manifest"
        )
        assets_bytes = _read_file_bounded(assets_path, 16 * 1024 * 1024, label="asset manifest")
        receipt_bytes = _read_file_bounded(
            receipt_path, DEFAULT_MAX_MANIFEST_BYTES, label="import receipt"
        )
        receipt = json.loads(receipt_bytes)
        assert_no_secrets(receipt)
        if not isinstance(receipt, dict) or receipt.get("contract") != IMPORT_CONTRACT:
            errors.append("import receipt contract is invalid")
        if receipt.get("package_id") != manifest.package_id:
            errors.append("import receipt package_id mismatch")
        if receipt.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest():
            errors.append("source package manifest hash mismatch")
        if receipt.get("assets_sha256") != hashlib.sha256(assets_bytes).hexdigest():
            errors.append("asset manifest hash mismatch")
        payload = json.loads(assets_bytes)
        assert_no_secrets(payload)
        if not isinstance(payload, dict) or payload.get("contract") != ASSET_CONTRACT:
            raise ValueError("asset manifest contract is invalid")
        records = payload.get("assets", [])
        if not isinstance(records, list):
            raise ValueError("asset manifest assets must be an array")
        if len(records) != len(manifest.items):
            errors.append("source item to asset count mismatch")
        expected_by_identity = {
            (manifest.package_id, item.item_id): item for item in manifest.items
        }
        seen_ids: set[str] = set()
        seen_identities: set[tuple[str, str]] = set()
        declared_paths: set[str] = set()
        verified_bytes = 0
        for record in records:
            if not isinstance(record, dict):
                errors.append("asset record is invalid")
                continue
            digest = record.get("sha256")
            asset_id = record.get("asset_id")
            blob_id = record.get("blob_id")
            source_id = record.get("source_id")
            item_id = record.get("item_id")
            relative_path = record.get("relative_path")
            if not all(
                isinstance(value, str)
                for value in (digest, asset_id, blob_id, source_id, item_id, relative_path)
            ) or not _DIGEST.fullmatch(cast(str, digest)):
                errors.append("asset record identity is invalid")
                continue
            digest = cast(str, digest)
            asset_id = cast(str, asset_id)
            source_id = cast(str, source_id)
            item_id = cast(str, item_id)
            relative_path = cast(str, relative_path)
            identity = (source_id, item_id)
            item = expected_by_identity.get(identity)
            if asset_id in seen_ids:
                errors.append(f"duplicate asset_id {asset_id}")
            seen_ids.add(asset_id)
            if identity in seen_identities:
                errors.append(f"duplicate source identity {source_id}:{item_id}")
            seen_identities.add(identity)
            expected_path = canonical_blob_path(digest).as_posix()
            expected_id = asset_id_for(
                digest,
                source_id=source_id,
                item_id=item_id,
                source_url=str(record.get("source_url", "")),
                role=str(record.get("role", "")),
            )
            if relative_path != expected_path or blob_id != f"blob-{digest}":
                errors.append(f"asset blob identity mismatch: {asset_id}")
            if asset_id != expected_id:
                errors.append(f"asset provenance identity mismatch: {asset_id}")
            if item is None:
                errors.append(f"asset has no source item: {source_id}:{item_id}")
            else:
                expected_url = (
                    sanitize_url_for_persistence(item.source_url) if item.source_url else ""
                )
                if (
                    digest != item.sha256
                    or record.get("byte_size") != item.byte_size
                    or record.get("media_type") != item.mime.lower()
                    or record.get("role") != item.modality
                    or record.get("source_url") != expected_url
                    or record.get("alt") != str(sanitize_metadata(item.title))
                ):
                    errors.append(f"asset does not match source item: {item.item_id}")
                policy = record.get("source_policy")
                if (
                    not isinstance(policy, Mapping)
                    or SourcePolicy.from_mapping(policy) != manifest.source_policy
                ):
                    errors.append(f"asset policy mismatch: {item.item_id}")
            declared_paths.add(relative_path)
            path = root / relative_path
            if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
                errors.append("asset path is missing or unsafe")
                continue
            declared_size = record.get("byte_size")
            if not isinstance(declared_size, int) or isinstance(declared_size, bool):
                errors.append(f"asset size is invalid: {asset_id}")
                continue
            body = _read_file_bounded(
                path, max(declared_size, 1), label=f"imported asset {asset_id}"
            )
            if hashlib.sha256(body).hexdigest() != record.get("sha256"):
                errors.append(f"asset hash mismatch: {asset_id}")
            if len(body) != declared_size:
                errors.append(f"asset size mismatch: {asset_id}")
            verified_bytes += declared_size
        if receipt.get("asset_count") != len(records):
            errors.append("asset count mismatch")
        if receipt.get("total_bytes") != sum(item.byte_size for item in manifest.items):
            errors.append("import receipt total_bytes mismatch")
        if verified_bytes != sum(item.byte_size for item in manifest.items):
            errors.append("verified asset total_bytes mismatch")
        if seen_identities != set(expected_by_identity):
            errors.append("source item to asset bijection mismatch")
        blob_root = root / "_assets" / "sha256"
        actual_paths: set[str] = set()
        if blob_root.exists():
            for candidate in blob_root.rglob("*"):
                if candidate.is_symlink():
                    errors.append("asset tree contains a symlink")
                elif candidate.is_file():
                    actual_paths.add(candidate.relative_to(root).as_posix())
        if actual_paths != declared_paths:
            errors.append("declared asset blobs do not match stored blobs")
        return VerificationResult(not errors, len(records), tuple(errors))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return VerificationResult(False, 0, (f"invalid import: {exc}",))


def assert_no_secrets(value: object, path: str = "manifest") -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_secrets(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if _SECRET_FIELD.fullmatch(str(key)):
                raise ValueError(f"source package contains forbidden secret field at {path}.{key}")
            assert_no_secrets(item, f"{path}.{key}")
        return
    if isinstance(value, str):
        if (
            _CREDENTIAL_VALUE.search(value)
            or re.search(r"\b(?:Cookie|Set-Cookie|Proxy-Authorization)\s*:", value, re.I)
            or redact_sensitive_text(value) != value
        ):
            raise ValueError(f"source package contains a credential-like value at {path}")
        if _SESSION_PATH.search(value):
            raise ValueError(f"source package references browser/session state at {path}")
        if value.lower().startswith(("http://", "https://")):
            inspection = inspect_url_credentials(value)
            if inspection.has_userinfo or inspection.sensitive_query_keys:
                raise ValueError(f"source package contains a credential-bearing URL at {path}")


def _assert_text_payload_has_no_secrets(body: bytes, *, mime: str, path: str) -> None:
    if not (
        mime.lower().startswith("text/")
        or mime.lower() in {"application/json", "application/x-ndjson"}
    ):
        return
    try:
        text = body.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"source package text item is not UTF-8: {path}") from exc
    assert_no_secrets(text, f"item:{path}")


def _item_from_mapping(value: Mapping[str, object]) -> SourcePackageItem:
    metadata = value.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("source package item metadata must be an object")
    byte_size = value.get("byte_size")
    if not isinstance(byte_size, int) or isinstance(byte_size, bool):
        raise ValueError("source package item byte_size is required")
    return SourcePackageItem(
        item_id=_required_string(value, "item_id"),
        path=_required_string(value, "path"),
        modality=cast(Modality, _required_string(value, "modality")),
        mime=_required_string(value, "mime"),
        title=_required_string(value, "title"),
        sha256=_required_string(value, "sha256"),
        byte_size=byte_size,
        source_url=_optional_string(value, "source_url"),
        transcript_path=_optional_string(value, "transcript_path"),
        metadata=cast(dict[str, object], sanitize_metadata(metadata)),
    )


def _contained_regular_file(root: Path, relative: str) -> Path:
    _validate_relative_path(relative)
    candidate = root / relative
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("source package items must be regular non-symlink files")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("source package item resolves outside package root")
    return resolved


def _validate_relative_path(value: str) -> None:
    path = Path(value)
    if (
        not value
        or value == "."
        or "\\" in value
        or path.is_absolute()
        or "\0" in value
        or ".." in path.parts
    ):
        raise ValueError("source package item paths must be contained relative paths")
    if _SESSION_PATH.search(value):
        raise ValueError("source package must not include browser/session state")


def _read_metadata(path: Path, root: Path) -> dict[str, dict[str, object]]:
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("source package metadata path is unsafe")
    result: dict[str, dict[str, object]] = {}
    body = _read_file_bounded(path, DEFAULT_MAX_MANIFEST_BYTES, label="source package metadata")
    try:
        text = body.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("source package metadata must be UTF-8") from exc
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        assert_no_secrets(value)
        if not isinstance(value, dict) or not isinstance(value.get("path"), str):
            raise ValueError(f"invalid source metadata at line {line_number}")
        _validate_relative_path(value["path"])
        result[value["path"]] = value
    return result


def _looks_like_source_manifest(
    path: Path, *, max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES
) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        if path.stat(follow_symlinks=False).st_size > max_manifest_bytes:
            return False
        value = json.loads(
            _read_file_bounded(path, max_manifest_bytes, label="possible source package manifest")
        )
        return isinstance(value, dict) and value.get("contract") == PACKAGE_CONTRACT
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _canonical_manifest_bytes(manifest: SourcePackageManifest) -> bytes:
    return (json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")


def _assert_manifest_serialized_size(
    manifest: SourcePackageManifest, max_manifest_bytes: int
) -> None:
    if len(_canonical_manifest_bytes(manifest)) > max_manifest_bytes:
        raise ValueError(f"source package manifest exceeds {max_manifest_bytes} bytes")


def _read_file_bounded(path: Path, max_bytes: int, *, label: str) -> bytes:
    """Read one regular file through a no-follow descriptor under a hard cap."""

    if max_bytes <= 0:
        raise ValueError(f"{label} byte limit must be positive")
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"{label} must be a regular file")
    if before.st_size > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"{label} must be a regular file")
        if before.st_dev != opened.st_dev or before.st_ino != opened.st_ino:
            raise ValueError(f"{label} changed before it was opened")
        if opened.st_size > max_bytes:
            raise ValueError(f"{label} exceeds {max_bytes} bytes")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"{label} exceeds {max_bytes} bytes")
        finished = os.fstat(descriptor)
        if (
            opened.st_dev != finished.st_dev
            or opened.st_ino != finished.st_ino
            or opened.st_size != finished.st_size
            or total != finished.st_size
        ):
            raise ValueError(f"{label} changed while it was being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"source package {key} is required")
    return item


def _optional_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key, "")
    if not isinstance(item, str):
        raise ValueError(f"source package {key} must be a string")
    return item


def _string_override(value: Mapping[str, object], key: str, default: str) -> str:
    item = value.get(key, default)
    if not isinstance(item, str):
        raise ValueError(f"source metadata {key} must be a string")
    return item


def _parse_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("source package created_at must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("source package created_at must include a timezone")


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".json-", delete=False
    ) as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _supported_extension(extension: str) -> bool:
    return extension.lower() in {
        ".md",
        ".txt",
        ".html",
        ".htm",
        ".json",
        ".jsonl",
        ".csv",
        ".vtt",
        ".srt",
        ".pdf",
        ".docx",
        ".pptx",
        ".xlsx",
        ".epub",
        ".png",
        ".jpg",
        ".jpeg",
        ".svg",
        ".webp",
        ".gif",
        ".tif",
        ".tiff",
        ".mp3",
        ".wav",
        ".m4a",
        ".mp4",
        ".mov",
        ".webm",
    }


def _mime_from_path(path: Path) -> str:
    return {
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".html": "text/html",
        ".htm": "text/html",
        ".json": "application/json",
        ".jsonl": "application/x-ndjson",
        ".csv": "text/csv",
        ".vtt": "text/vtt",
        ".srt": "application/x-subrip",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".epub": "application/epub+zip",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".svg": "image/svg+xml",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
    }.get(path.suffix.lower(), "application/octet-stream")


def _modality_from_mime(mime: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    if mime in {"application/pdf", "application/epub+zip"} or "officedocument" in mime:
        return "document"
    return "text"
