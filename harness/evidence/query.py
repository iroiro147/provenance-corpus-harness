"""Deterministic local text/image retrieval that always returns citations."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from ..rights import AuthorizationDeclaration
from .build import INDEX_SCHEMA, _canonical, _text_index, resolve_index_generation
from .schema import (
    EvidenceAsset,
    EvidenceCitation,
    EvidenceUnit,
    RightsDeclaration,
    VisualDescriptor,
    to_dict,
)
from .visual import describe_image, visual_similarity

_TOKEN = re.compile(r"[\w'-]+", re.UNICODE)


@dataclass(frozen=True)
class QueryResult:
    index_id: str
    citations: tuple[EvidenceCitation, ...]
    capabilities: dict[str, bool]


def query_evidence_index(
    index_dir: str | Path,
    *,
    text: str | None = None,
    image_path: str | Path | None = None,
    limit: int = 10,
    modalities: set[str] | None = None,
    access_classes: set[str] | None = None,
    max_per_source: int = 3,
) -> QueryResult:
    if not (text and text.strip()) and image_path is None:
        raise ValueError("evidence query requires text or image_path")
    if limit <= 0 or max_per_source <= 0:
        raise ValueError("limit and max_per_source must be positive")
    root = resolve_index_generation(index_dir)
    manifest = _read_json(root / "index-manifest.json")
    assets = [_asset(value) for value in _read_jsonl(root / "assets.jsonl")]
    units = [_unit(value) for value in _read_jsonl(root / "evidence.jsonl")]
    descriptors = [_descriptor(value) for value in _read_jsonl(root / "visual-index.jsonl")]
    derivatives = _read_jsonl(root / "derivatives.jsonl")
    text_index = _read_json(root / "text-index.json")
    _validate_internal_index(manifest, assets, units, derivatives, descriptors, text_index)
    asset_map = {value.asset_id: value for value in assets}
    unit_map = {value.evidence_id: value for value in units}
    allowed = {
        unit.evidence_id
        for unit in units
        if (asset := asset_map.get(unit.asset_id))
        and not asset.tombstoned_at
        and (modalities is None or asset.modality in modalities)
        and (access_classes is None or asset.access_class in access_classes)
    }
    text_rank = _query_text(root / "text-index.json", text or "", allowed)
    visual_rank = []
    if image_path is not None:
        query_descriptor = describe_image(image_path, evidence_id="query", asset_id="query")
        visual_rank = sorted(
            (
                (descriptor.evidence_id, visual_similarity(query_descriptor, descriptor))
                for descriptor in descriptors
                if descriptor.evidence_id in allowed
            ),
            key=lambda value: (-value[1], value[0]),
        )
    fused = _fuse(text_rank, visual_rank)
    selected = []
    source_counts: dict[str, int] = {}
    for evidence_id, fused_score, text_score, visual_score in fused:
        unit = unit_map[evidence_id]
        asset = asset_map[unit.asset_id]
        source = asset.safe_source_url or asset.source_id
        if source_counts.get(source, 0) >= max_per_source:
            continue
        source_counts[source] = source_counts.get(source, 0) + 1
        selected.append((unit, asset, fused_score, text_score, visual_score))
        if len(selected) >= limit:
            break
    citations = tuple(
        EvidenceCitation(
            index_id=str(manifest["index_id"]),
            evidence_id=unit.evidence_id,
            asset_id=asset.asset_id,
            rank=index + 1,
            fused_score=fused_score,
            text_score=text_score,
            visual_score=visual_score,
            matched_modalities=tuple(
                value
                for value, score in (("text", text_score), ("visual", visual_score))
                if score is not None
            ),
            title=unit.title,
            snippet=_snippet(unit.text, text),
            locator=unit.locator,
            record_path=asset.record_path,
            original_path=asset.local_path,
            derivative_path=unit.derivative_path,
            safe_source_url=asset.safe_source_url,
            rights=asset.rights,
            authorization=asset.authorization,
            access_class=asset.access_class,
            local_only=asset.local_only,
            source_sha256=asset.sha256,
            evidence_sha256=unit.content_hash,
        )
        for index, (unit, asset, fused_score, text_score, visual_score) in enumerate(selected)
    )
    return QueryResult(str(manifest["index_id"]), citations, dict(manifest["capabilities"]))


def _query_text(path: Path, query: str, allowed: set[str]):
    if not query.strip():
        return []
    index = _read_json(path)
    terms = [match.group(0).lower() for match in _TOKEN.finditer(query)]
    count = int(index["document_count"])
    average = float(index["average_length"]) or 1.0
    frequencies = index["document_frequency"]
    results = []
    for document in index["documents"]:
        evidence_id = str(document["evidence_id"])
        if evidence_id not in allowed:
            continue
        score = 0.0
        length = int(document["length"])
        for term in terms:
            tf = int(document["terms"].get(term, 0))
            if not tf:
                continue
            df = int(frequencies[term])
            inverse = math.log(1 + (count - df + 0.5) / (df + 0.5))
            score += inverse * tf * 2.2 / (tf + 1.2 * (1 - 0.75 + 0.75 * length / average))
        if score:
            results.append((evidence_id, round(score, 10)))
    return sorted(results, key=lambda value: (-value[1], value[0]))


def _fuse(text_rank, visual_rank):
    rows: dict[str, list[float | None]] = {}
    for kind, ranking in (("text", text_rank), ("visual", visual_rank)):
        for position, (evidence_id, score) in enumerate(ranking, 1):
            row = rows.setdefault(evidence_id, [0.0, None, None])
            row[0] = float(row[0]) + 1 / (60 + position)
            row[1 if kind == "text" else 2] = score
    return sorted(
        ((key, round(float(value[0]), 10), value[1], value[2]) for key, value in rows.items()),
        key=lambda value: (-value[1], value[0]),
    )


def _snippet(value: str, query: str | None) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return "Visual evidence"
    term = next(iter(_TOKEN.finditer(query or "")), None)
    location = text.lower().find(term.group(0).lower()) if term else 0
    start = max(0, location - 80) if location >= 0 else 0
    return text[start : start + 360]


def _asset(value: dict) -> EvidenceAsset:
    data = dict(value)
    data["rights"] = RightsDeclaration.from_value(
        data.get("rights"), access_class=data.get("access_class", "unknown")
    )
    authorization = data.get("authorization")
    data["authorization"] = (
        AuthorizationDeclaration.from_mapping(authorization)
        if isinstance(authorization, dict)
        else None
    )
    return EvidenceAsset(**data)


def _unit(value: dict) -> EvidenceUnit:
    return EvidenceUnit(**value)


def _descriptor(value: dict) -> VisualDescriptor:
    data = dict(value)
    data["color_grid"] = tuple(data["color_grid"])
    return VisualDescriptor(**data)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _validate_internal_index(
    manifest: dict,
    assets: list[EvidenceAsset],
    units: list[EvidenceUnit],
    derivatives: list[dict],
    descriptors: list[VisualDescriptor],
    text_index: dict,
) -> None:
    if manifest.get("schema_version") != INDEX_SCHEMA:
        raise ValueError("evidence index schema version is unsupported")
    expected_text = _text_index(units)
    if text_index != expected_text:
        raise ValueError("evidence text index does not match its records")
    content = {
        "assets": [to_dict(value) for value in assets],
        "evidence": [to_dict(value) for value in units],
        "derivatives": derivatives,
        "providers": manifest.get("providers"),
        "visual": [to_dict(value) for value in descriptors],
    }
    content_hash = hashlib.sha256(_canonical(content)).hexdigest()
    if manifest.get("content_hash") != content_hash:
        raise ValueError("evidence index content hash does not match its records")
    if manifest.get("index_id") != f"evidence-{content_hash[:20]}":
        raise ValueError("evidence index ID does not match its records")
