"""Integrity verification for portable evidence indexes."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .build import INDEX_SCHEMA, _text_index, build_evidence_index, resolve_index_generation
from .schema import EvidenceUnit, VisualDescriptor, to_dict
from .visual import VisualDependencyError, describe_image


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    index_id: str | None
    checks: int
    errors: tuple[str, ...]
    warnings: tuple[str, ...] = ()


def verify_evidence_index(index_dir: str | Path, *, corpus_dir: str | Path) -> VerifyResult:
    try:
        index = resolve_index_generation(index_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return VerifyResult(False, None, 1, (f"index pointer cannot be loaded: {exc}",))
    corpus = Path(corpus_dir).expanduser().resolve()
    errors: list[str] = []
    checks = 0
    try:
        manifest = _read_json(index / "index-manifest.json")
        assets = _read_jsonl(index / "assets.jsonl")
        evidence = _read_jsonl(index / "evidence.jsonl")
        descriptors = _read_jsonl(index / "visual-index.jsonl")
        derivatives = _read_jsonl(index / "derivatives.jsonl")
        text_index = _read_json(index / "text-index.json")
        checks += 6
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return VerifyResult(False, None, 1, (f"index cannot be loaded: {exc}",))
    asset_ids = [row.get("asset_id") for row in assets]
    evidence_ids = [row.get("evidence_id") for row in evidence]
    checks += 2
    if len(asset_ids) != len(set(asset_ids)):
        errors.append("asset IDs are not unique")
    if len(evidence_ids) != len(set(evidence_ids)):
        errors.append("evidence IDs are not unique")
    asset_set = set(asset_ids)
    evidence_set = set(evidence_ids)
    asset_map = {row.get("asset_id"): row for row in assets if isinstance(row.get("asset_id"), str)}
    checks += 1
    if manifest.get("schema_version") != INDEX_SCHEMA:
        errors.append("index manifest schema version is unsupported")
    _verify_source_derived_records(
        corpus,
        manifest=manifest,
        assets=assets,
        evidence=evidence,
        errors=errors,
    )
    checks += 2
    for asset in assets:
        path = _contained_regular_file(corpus, asset.get("local_path"), errors)
        checks += 1
        if path is None:
            continue
        data = path.read_bytes()
        checks += 2
        if len(data) != asset.get("bytes"):
            errors.append(f"asset byte count changed: {asset.get('asset_id')}")
        if hashlib.sha256(data).hexdigest() != asset.get("sha256"):
            errors.append(f"asset hash changed: {asset.get('asset_id')}")
    for unit in evidence:
        checks += 2
        if unit.get("asset_id") not in asset_set:
            errors.append(f"evidence references missing asset: {unit.get('evidence_id')}")
        expected = hashlib.sha256(str(unit.get("text", "")).encode()).hexdigest()
        if unit.get("kind") == "text" and unit.get("content_hash") != expected:
            errors.append(f"evidence content hash changed: {unit.get('evidence_id')}")
        if unit.get("kind") == "image":
            asset = asset_map.get(unit.get("asset_id"))
            expected_image = (
                hashlib.sha256(str(asset.get("sha256")).encode()).hexdigest()
                if asset is not None
                else None
            )
            if unit.get("content_hash") != expected_image:
                errors.append(f"image evidence hash changed: {unit.get('evidence_id')}")
    for descriptor in descriptors:
        checks += 2
        if descriptor.get("asset_id") not in asset_set:
            errors.append("visual descriptor references a missing asset")
        if descriptor.get("evidence_id") not in evidence_set:
            errors.append("visual descriptor references missing evidence")
        asset = asset_map.get(descriptor.get("asset_id"))
        if asset is None:
            continue
        source = _contained_regular_file(corpus, asset.get("local_path"), errors)
        checks += 1
        if source is None:
            continue
        try:
            expected_descriptor = describe_image(
                source,
                evidence_id=str(descriptor.get("evidence_id")),
                asset_id=str(descriptor.get("asset_id")),
            )
            actual_descriptor = VisualDescriptor(
                **{
                    **descriptor,
                    "color_grid": tuple(descriptor.get("color_grid", ())),
                }
            )
            checks += 4
            if actual_descriptor.dhash != expected_descriptor.dhash:
                errors.append("visual descriptor perceptual hash changed")
            if actual_descriptor.color_grid != expected_descriptor.color_grid:
                errors.append("visual descriptor color values changed")
            if actual_descriptor.width != expected_descriptor.width:
                errors.append("visual descriptor width changed")
            if actual_descriptor.height != expected_descriptor.height:
                errors.append("visual descriptor height changed")
            if actual_descriptor.image_path != asset.get("local_path"):
                errors.append("visual descriptor path does not match its asset")
        except (OSError, TypeError, ValueError, VisualDependencyError) as exc:
            errors.append(f"visual descriptor cannot be rederived: {exc}")
    try:
        expected_text_index = _text_index([EvidenceUnit(**row) for row in evidence])
        checks += 1
        if text_index != expected_text_index:
            errors.append("text index does not match evidence records")
    except (TypeError, ValueError, KeyError) as exc:
        errors.append(f"text index cannot be verified: {exc}")
    content = {
        "assets": assets,
        "evidence": evidence,
        "derivatives": derivatives,
        "providers": manifest.get("providers"),
        "visual": descriptors,
    }
    content_hash = hashlib.sha256(_canonical(content)).hexdigest()
    checks += 7
    if manifest.get("content_hash") != content_hash:
        errors.append("manifest content hash does not match index records")
    if manifest.get("index_id") != f"evidence-{content_hash[:20]}":
        errors.append("manifest index ID does not match indexed content")
    counts = manifest.get("counts", {})
    expected_counts = {
        "assets": len(assets),
        "evidence": len(evidence),
        "text_units": sum(bool(row.get("text", "").strip()) for row in evidence),
        "visual_units": len(descriptors),
        "derivatives": len(derivatives),
    }
    for key, value in expected_counts.items():
        if counts.get(key) != value:
            errors.append(f"manifest {key} count does not match index records")
    return VerifyResult(
        not errors,
        str(manifest.get("index_id")) if manifest.get("index_id") else None,
        checks,
        tuple(errors),
    )


def _verify_source_derived_records(
    corpus: Path,
    *,
    manifest: dict,
    assets: list[dict],
    evidence: list[dict],
    errors: list[str],
) -> None:
    """Rebuild source-derived records so a self-consistent rewrite cannot pass verification."""
    try:
        providers = manifest.get("providers")
        if not isinstance(providers, dict) or not isinstance(providers.get("text"), dict):
            raise ValueError("manifest has no text provider configuration")
        text_provider = providers["text"]
        chunk_chars = text_provider.get("chunk_chars")
        chunk_overlap = text_provider.get("chunk_overlap")
        if (
            not isinstance(chunk_chars, int)
            or isinstance(chunk_chars, bool)
            or not isinstance(chunk_overlap, int)
            or isinstance(chunk_overlap, bool)
        ):
            raise ValueError("manifest text chunk configuration is invalid")
        with tempfile.TemporaryDirectory(prefix="corpus-harness-verify-") as temporary:
            rebuilt = build_evidence_index(
                corpus,
                Path(temporary) / "index",
                chunk_chars=chunk_chars,
                chunk_overlap=chunk_overlap,
                strict=True,
                now=manifest.get("created_at"),
            )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"source records cannot be rederived: {exc}")
        return

    expected_assets = [_json_value(to_dict(asset)) for asset in rebuilt.assets]
    if assets != expected_assets:
        errors.append("indexed asset provenance and policy do not match corpus records")

    expected_text = [_json_value(to_dict(unit)) for unit in rebuilt.evidence if unit.kind == "text"]
    actual_text = [unit for unit in evidence if unit.get("kind") == "text"]
    if actual_text != expected_text:
        errors.append("indexed text evidence does not match corpus source bytes and locators")


def _contained_regular_file(root: Path, value: object, errors: list[str]) -> Path | None:
    if not isinstance(value, str) or not value:
        errors.append("asset has no local path")
        return None
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        errors.append(f"asset path is not relative: {value}")
        return None
    path = root / relative
    if path.is_symlink() or not path.exists() or not path.resolve().is_relative_to(root):
        errors.append(f"asset is missing or escapes corpus: {value}")
        return None
    if not path.is_file():
        errors.append(f"asset is not a regular file: {value}")
        return None
    return path


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _json_value(value: object):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
