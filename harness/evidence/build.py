"""Build a deterministic, local evidence index from provenance corpus records."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

from ..rights import AuthorizationDeclaration
from ..url_safety import canonicalize_url, sanitize_metadata
from .schema import (
    EvidenceAsset,
    EvidenceUnit,
    RightsDeclaration,
    VisualDescriptor,
    _access_class,
    to_dict,
)
from .visual import VisualDependencyError, describe_image, visual_provider_metadata

INDEX_SCHEMA = "provenance-evidence-index.v1"
ASSET_MANIFEST = Path("_provenance/assets.jsonl")
PORTABLE_ASSET_MANIFEST = Path("assets.json")
_TOKEN = re.compile(r"[\w'-]+", re.UNICODE)


@dataclass(frozen=True)
class IndexBuildResult:
    index_dir: Path
    index_id: str
    assets: tuple[EvidenceAsset, ...]
    evidence: tuple[EvidenceUnit, ...]
    visual_descriptors: tuple[VisualDescriptor, ...]
    warnings: tuple[str, ...]


def build_evidence_index(
    corpus_dir: str | Path,
    index_dir: str | Path,
    *,
    chunk_chars: int = 1600,
    chunk_overlap: int = 200,
    strict: bool = False,
    now=None,
) -> IndexBuildResult:
    if chunk_chars < 64 or chunk_overlap < 0 or chunk_overlap >= chunk_chars:
        raise ValueError("chunk_chars must be >= 64 and overlap must be smaller")
    corpus = Path(corpus_dir).expanduser().resolve()
    output = Path(index_dir).expanduser().resolve()
    if not corpus.is_dir():
        raise ValueError("corpus_dir must be an existing directory")
    output.mkdir(parents=True, exist_ok=True)
    assets: list[EvidenceAsset] = []
    units: list[EvidenceUnit] = []
    descriptors: list[VisualDescriptor] = []
    warnings: list[str] = []

    markdown_paths = sorted(corpus.rglob("*.md"))
    invalid_paths = [
        path
        for path in markdown_paths
        if path.is_symlink() or not path.resolve().is_relative_to(corpus)
    ]
    if invalid_paths and strict:
        relative = invalid_paths[0].relative_to(corpus).as_posix()
        raise ValueError(f"record path is not a contained regular file: {relative}")
    for path in markdown_paths:
        if output == path or output in path.parents or "_provenance" in path.parts:
            continue
        record = _load_markdown_record(corpus, path, strict=strict, warnings=warnings)
        if record is None:
            continue
        asset, body = record
        assets.append(asset)
        for start, end, text in _chunks(body, chunk_chars, chunk_overlap):
            content_hash = _sha256(text.encode())
            evidence_id = f"evidence-{_stable_hash(asset.asset_id, start, end, content_hash)[:20]}"
            units.append(
                EvidenceUnit(
                    evidence_id=evidence_id,
                    asset_id=asset.asset_id,
                    kind="text",
                    title=asset.title,
                    text=text,
                    locator={"char_start": start, "char_end": end},
                    content_hash=content_hash,
                )
            )

    for raw in _asset_manifest_rows(corpus):
        try:
            asset = _manifest_asset(corpus, raw)
            if any(existing.asset_id == asset.asset_id for existing in assets):
                raise ValueError(f"duplicate asset_id {asset.asset_id}")
            assets.append(asset)
            if asset.tombstoned_at:
                continue
            if asset.modality == "image":
                evidence_id = f"evidence-{_stable_hash(asset.asset_id, 'image')[:20]}"
                unit = EvidenceUnit(
                    evidence_id=evidence_id,
                    asset_id=asset.asset_id,
                    kind="image",
                    title=asset.title,
                    text=str(asset.metadata.get("alt") or asset.metadata.get("caption") or ""),
                    content_hash=_sha256(asset.sha256.encode()),
                )
                units.append(unit)
                try:
                    descriptor = describe_image(
                        corpus / asset.local_path,
                        evidence_id=evidence_id,
                        asset_id=asset.asset_id,
                    )
                    descriptors.append(
                        VisualDescriptor(**{**to_dict(descriptor), "image_path": asset.local_path})
                    )
                except VisualDependencyError as exc:
                    warnings.append(str(exc))
                except (OSError, ValueError) as exc:
                    message = f"{asset.asset_id}: image descriptor failed: {exc}"
                    if strict:
                        raise ValueError(message) from exc
                    warnings.append(message)
            elif asset.modality == "text" or (
                asset.modality == "document" and asset.mime.startswith("text/")
            ):
                try:
                    body = (corpus / asset.local_path).read_text(encoding="utf-8").strip()
                except UnicodeError as exc:
                    raise ValueError(f"{asset.asset_id}: text asset is not UTF-8") from exc
                for start, end, text in _chunks(body, chunk_chars, chunk_overlap):
                    content_hash = _sha256(text.encode())
                    evidence_id = (
                        f"evidence-{_stable_hash(asset.asset_id, start, end, content_hash)[:20]}"
                    )
                    units.append(
                        EvidenceUnit(
                            evidence_id=evidence_id,
                            asset_id=asset.asset_id,
                            kind="text",
                            title=asset.title,
                            text=text,
                            locator={"char_start": start, "char_end": end},
                            content_hash=content_hash,
                        )
                    )
        except (TypeError, ValueError, OSError) as exc:
            if strict:
                raise
            warnings.append(f"asset manifest row skipped: {exc}")

    assets.sort(key=lambda value: value.asset_id)
    units.sort(key=lambda value: value.evidence_id)
    descriptors.sort(key=lambda value: value.evidence_id)
    text_index = _text_index(units)
    providers = {
        "text": {
            "id": "bm25-local-v1",
            "algorithm_version": 1,
            "tokenizer": "python-unicode-word-v1",
            "chunk_chars": chunk_chars,
            "chunk_overlap": chunk_overlap,
        },
        "visual": visual_provider_metadata() if descriptors else None,
    }
    content = {
        "assets": [to_dict(value) for value in assets],
        "evidence": [to_dict(value) for value in units],
        "derivatives": [],
        "providers": providers,
        "visual": [to_dict(value) for value in descriptors],
    }
    content_hash = _sha256(_canonical(content))
    index_id = f"evidence-{content_hash[:20]}"
    created_at = _timestamp(now)
    manifest = {
        "schema_version": INDEX_SCHEMA,
        "index_id": index_id,
        "created_at": created_at,
        "content_hash": content_hash,
        "providers": providers,
        "capabilities": {
            "text_query": any(unit.text.strip() for unit in units),
            "image_query": bool(descriptors),
            "semantic_text_to_visual": False,
        },
        "counts": {
            "assets": len(assets),
            "evidence": len(units),
            "text_units": sum(bool(unit.text.strip()) for unit in units),
            "visual_units": len(descriptors),
            "derivatives": 0,
        },
    }
    generation = _publish_generation(
        output,
        index_id,
        manifest=manifest,
        assets=(to_dict(value) for value in assets),
        evidence=(to_dict(value) for value in units),
        descriptors=(to_dict(value) for value in descriptors),
        text_index=text_index,
        warnings=warnings,
    )
    return IndexBuildResult(
        generation, index_id, tuple(assets), tuple(units), tuple(descriptors), tuple(warnings)
    )


def _load_markdown_record(corpus: Path, path: Path, *, strict: bool, warnings: list[str]):
    relative = path.relative_to(corpus).as_posix()
    if path.is_symlink() or not path.resolve().is_relative_to(corpus):
        message = f"record path is not a contained regular file: {relative}"
        if strict:
            raise ValueError(message)
        warnings.append(message)
        return None
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    parts = text.split("---", 2)
    if len(parts) != 3:
        message = f"record has no YAML frontmatter: {relative}"
        if strict:
            raise ValueError(message)
        warnings.append(message)
        return None
    front = yaml.safe_load(parts[1])
    if not isinstance(front, dict):
        raise ValueError(f"record frontmatter is not a mapping: {relative}")
    title = str(sanitize_metadata(front.get("title", "")))
    rendered = parts[2].lstrip("\n")
    heading = f"# {title}\n\n" if title else ""
    body = rendered[len(heading) :] if heading and rendered.startswith(heading) else rendered
    body = body.strip()
    actual_content_hash = _sha256(body.encode())
    declared = front.get("content_hash")
    if declared != actual_content_hash:
        message = f"record content hash mismatch: {relative}"
        if strict:
            raise ValueError(message)
        warnings.append(message)
        return None
    source_url = front.get("source_url")
    safe_url = canonicalize_url(source_url) if isinstance(source_url, str) else None
    access = _access_class(front.get("access_class", "unknown"))
    rights_value = sanitize_metadata(front.get("rights", {}))
    rights = RightsDeclaration.from_value(rights_value, access_class=access)
    record_hash = _sha256(raw)
    asset_id = f"asset-{_stable_hash(relative, record_hash)[:20]}"
    return (
        EvidenceAsset(
            asset_id=asset_id,
            source_id=f"source-{_stable_hash(safe_url or relative)[:20]}",
            record_path=relative,
            local_path=relative,
            sha256=record_hash,
            bytes=len(raw),
            mime="text/markdown",
            modality="text",
            title=title,
            safe_source_url=safe_url,
            rights=rights,
            authorization=None,
            access_class=access,
            metadata={},
        ),
        body,
    )


def _manifest_asset(corpus: Path, raw: object) -> EvidenceAsset:
    if not isinstance(raw, dict):
        raise ValueError("asset row must be an object")
    local_path = _relative_path(raw.get("local_path"))
    record_path = _relative_path(raw.get("record_path", local_path))
    candidate = corpus / local_path
    path = candidate.resolve()
    if candidate.is_symlink() or not path.is_relative_to(corpus) or not path.is_file():
        raise ValueError(f"asset is not a contained regular file: {local_path}")
    data = path.read_bytes()
    actual_hash = _sha256(data)
    if raw.get("sha256") != actual_hash or raw.get("bytes") != len(data):
        raise ValueError(f"asset integrity mismatch: {local_path}")
    modality = raw.get("modality")
    if modality not in {"text", "document", "image", "audio", "video"}:
        raise ValueError(f"invalid modality: {modality}")
    source_url = raw.get("source_url") or raw.get("safe_source_url")
    safe_url = canonicalize_url(source_url) if isinstance(source_url, str) else None
    access = _access_class(raw.get("access_class", "unknown"))
    rights_value = sanitize_metadata(raw.get("rights", {}))
    rights = RightsDeclaration.from_value(rights_value, access_class=access)
    authorization_value = raw.get("authorization")
    authorization = (
        AuthorizationDeclaration.from_mapping(authorization_value)
        if isinstance(authorization_value, dict)
        else None
    )
    asset_id = raw.get("asset_id")
    if asset_id is None:
        asset_id = f"asset-{_stable_hash(local_path, actual_hash)[:20]}"
    else:
        asset_id = _opaque_id(asset_id, "asset_id")
    metadata = sanitize_metadata(raw.get("metadata", {}))
    if not isinstance(metadata, dict):
        raise ValueError("asset metadata must be an object")
    return EvidenceAsset(
        asset_id=asset_id,
        source_id=(
            _opaque_id(raw["source_id"], "source_id")
            if raw.get("source_id") is not None
            else f"source-{_stable_hash(safe_url or record_path)[:20]}"
        ),
        record_path=record_path,
        local_path=local_path,
        sha256=actual_hash,
        bytes=len(data),
        mime=str(
            raw.get("mime") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        ),
        modality=modality,
        title=str(sanitize_metadata(raw.get("title", path.stem))),
        safe_source_url=safe_url,
        rights=rights,
        authorization=authorization,
        access_class=access,
        local_only=bool(raw.get("local_only", True)),
        metadata=metadata,
        tombstoned_at=str(raw["tombstoned_at"]) if raw.get("tombstoned_at") else None,
    )


def _asset_manifest_rows(corpus: Path):
    jsonl_path = corpus / ASSET_MANIFEST
    if jsonl_path.exists():
        yield from _read_jsonl(jsonl_path)
    portable_path = corpus / PORTABLE_ASSET_MANIFEST
    if not portable_path.exists():
        return
    payload = json.loads(portable_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("contract") != "provenance-assets.v1":
        raise ValueError("assets.json has an unsupported contract")
    records = payload.get("assets")
    if not isinstance(records, list):
        raise ValueError("assets.json assets must be an array")
    for value in records:
        if not isinstance(value, dict):
            raise ValueError("assets.json records must be objects")
        policy = value.get("source_policy", {})
        if not isinstance(policy, dict):
            raise ValueError("asset source_policy must be an object")
        role = value.get("role")
        media_type = str(value.get("media_type", "application/octet-stream"))
        modality = (
            role
            if role in {"text", "document", "image", "audio", "video"}
            else _modality_from_mime(media_type)
        )
        yield {
            "asset_id": value.get("asset_id"),
            "record_path": "source-package.json"
            if (corpus / "source-package.json").is_file()
            else value.get("relative_path"),
            "local_path": value.get("relative_path"),
            "sha256": value.get("sha256"),
            "bytes": value.get("byte_size"),
            "mime": media_type,
            "modality": modality,
            "title": value.get("alt") or value.get("asset_id"),
            "source_url": value.get("source_url"),
            "access_class": policy.get("access_class", "unknown"),
            "rights": policy.get("rights", {}),
            "authorization": policy.get("authorization"),
            "local_only": policy.get("local_only", True),
            "metadata": {"alt": value.get("alt", ""), "role": role or "source"},
        }


def _modality_from_mime(value: str) -> str:
    if value.startswith("image/"):
        return "image"
    if value.startswith("audio/"):
        return "audio"
    if value.startswith("video/"):
        return "video"
    if value.startswith("text/"):
        return "text"
    return "document"


def _chunks(text: str, size: int, overlap: int):
    if not text:
        return
    start = 0
    while start < len(text):
        limit = min(len(text), start + size)
        end = limit
        if limit < len(text):
            boundary = max(
                text.rfind("\n\n", start + size // 2, limit),
                text.rfind(" ", start + size // 2, limit),
            )
            if boundary > start:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            left_trim = len(text[start:end]) - len(text[start:end].lstrip())
            right = end - (len(text[start:end]) - len(text[start:end].rstrip()))
            yield start + left_trim, right, chunk
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)


def _text_index(units: list[EvidenceUnit]) -> dict:
    documents = []
    document_frequency: dict[str, int] = {}
    for unit in units:
        terms = [match.group(0).lower() for match in _TOKEN.finditer(f"{unit.title} {unit.text}")]
        frequencies: dict[str, int] = {}
        for term in terms:
            frequencies[term] = frequencies.get(term, 0) + 1
        for term in frequencies:
            document_frequency[term] = document_frequency.get(term, 0) + 1
        documents.append(
            {
                "evidence_id": unit.evidence_id,
                "length": len(terms),
                "terms": dict(sorted(frequencies.items())),
            }
        )
    return {
        "schema_version": "provenance-bm25.v1",
        "document_count": len(documents),
        "average_length": round(sum(row["length"] for row in documents) / len(documents), 10)
        if documents
        else 0,
        "document_frequency": dict(sorted(document_frequency.items())),
        "documents": documents,
    }


def _relative_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("asset path must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("asset path must remain relative to the corpus")
    return path.as_posix()


def _opaque_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise ValueError(f"{label} must be an opaque 1-128 character identifier")
    return value


def _timestamp(now) -> str:
    value = now() if callable(now) else now
    value = value or datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("index timestamp must be timezone-aware")
        return value.isoformat()
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("index timestamp must be timezone-aware")
    return str(value)


def _stable_hash(*values: object) -> str:
    return _sha256(_canonical(values))


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_jsonl(path: Path):
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def _atomic_text(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json(path: Path, value: object) -> None:
    _atomic_text(path, json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def _write_jsonl(path: Path, values: Iterable[dict]) -> None:
    _atomic_text(
        path,
        "".join(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n" for value in values),
    )


def _publish_generation(
    root: Path,
    index_id: str,
    *,
    manifest: dict,
    assets: Iterable[dict],
    evidence: Iterable[dict],
    descriptors: Iterable[dict],
    text_index: dict,
    warnings: list[str],
) -> Path:
    generations = root / "generations"
    if generations.is_symlink():
        raise ValueError("evidence generation directory must not be a symlink")
    generations.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".generation-", dir=generations))
    destination = generations / index_id
    try:
        _write_jsonl(staging / "assets.jsonl", assets)
        _write_jsonl(staging / "evidence.jsonl", evidence)
        _write_jsonl(staging / "derivatives.jsonl", ())
        _write_jsonl(staging / "visual-index.jsonl", descriptors)
        _write_json(staging / "text-index.json", text_index)
        _write_json(staging / "build-warnings.json", warnings)
        _write_json(staging / "index-manifest.json", manifest)
        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise ValueError("existing evidence generation is unsafe")
            for name in (
                "assets.jsonl",
                "evidence.jsonl",
                "derivatives.jsonl",
                "visual-index.jsonl",
                "text-index.json",
            ):
                if (destination / name).read_bytes() != (staging / name).read_bytes():
                    raise ValueError("existing evidence generation conflicts with indexed content")
            existing_manifest = json.loads(
                (destination / "index-manifest.json").read_text(encoding="utf-8")
            )
            for key in ("schema_version", "index_id", "content_hash", "providers", "counts"):
                if existing_manifest.get(key) != manifest.get(key):
                    raise ValueError("existing evidence generation has conflicting metadata")
            shutil.rmtree(staging)
        else:
            os.replace(staging, destination)
        _write_json(
            root / "current.json",
            {
                "schema_version": "provenance-evidence-current.v1",
                "index_id": index_id,
                "generation": f"generations/{index_id}",
            },
        )
        return destination
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def resolve_index_generation(path: str | Path) -> Path:
    root = Path(path).expanduser().resolve()
    pointer = root / "current.json"
    if not pointer.exists():
        return root
    value = json.loads(pointer.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "provenance-evidence-current.v1"
    ):
        raise ValueError("evidence current pointer is invalid")
    relative = _relative_path(value.get("generation"))
    candidate = root / relative
    generation = candidate.resolve()
    if candidate.is_symlink() or not generation.is_relative_to(root) or not generation.is_dir():
        raise ValueError("evidence current pointer escapes its root")
    if generation.name != value.get("index_id"):
        raise ValueError("evidence current pointer index ID is inconsistent")
    return generation
