from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest
import yaml

from harness.assets import AssetStore, write_asset_manifest
from harness.base import CorpusItem, write_corpus_item
from harness.evidence import (
    build_evidence_index,
    query_evidence_index,
    verify_evidence_index,
)
from harness.evidence.build import _text_index
from harness.evidence.schema import EvidenceUnit
from harness.rights import AuthorizationDeclaration, RightsDeclaration, SourcePolicy

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _reseal_index(index_dir):
    assets = _jsonl(index_dir / "assets.jsonl")
    evidence = _jsonl(index_dir / "evidence.jsonl")
    derivatives = _jsonl(index_dir / "derivatives.jsonl")
    descriptors = _jsonl(index_dir / "visual-index.jsonl")
    (index_dir / "text-index.json").write_text(
        json.dumps(
            _text_index([EvidenceUnit(**row) for row in evidence]),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path = index_dir / "index-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    content = {
        "assets": assets,
        "evidence": evidence,
        "derivatives": derivatives,
        "providers": manifest["providers"],
        "visual": descriptors,
    }
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest["content_hash"] = digest
    manifest["index_id"] = f"evidence-{digest[:20]}"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_text_index_is_deterministic_and_returns_a_complete_citation(tmp_path):
    corpus = tmp_path / "corpus"
    record = write_corpus_item(
        CorpusItem(
            platform="blog",
            source_url="https://example.com/article?utm_source=test",
            title="Provenance Article",
            body="A durable corpus preserves source lineage.\n\nA second paragraph explains receipts.",
        ),
        corpus,
        scraped_at=NOW.isoformat(),
    )
    assert record
    first = build_evidence_index(corpus, tmp_path / "index-a", now=NOW)
    second = build_evidence_index(corpus, tmp_path / "index-b", now=NOW)
    assert first.index_id == second.index_id
    for name in ("assets.jsonl", "evidence.jsonl", "text-index.json", "index-manifest.json"):
        assert (first.index_dir / name).read_bytes() == (second.index_dir / name).read_bytes()

    result = query_evidence_index(first.index_dir, text="source lineage")
    assert result.citations
    citation = result.citations[0]
    assert citation.rank == 1
    assert citation.record_path == record.relative_to(corpus).as_posix()
    assert citation.original_path == citation.record_path
    assert citation.safe_source_url == "https://example.com/article"
    assert citation.rights.status == "unknown"
    assert citation.rights.permitted_uses == ("local-research",)
    assert citation.locator["char_end"] > citation.locator["char_start"]
    assert len(citation.source_sha256) == len(citation.evidence_sha256) == 64
    assert citation.matched_modalities == ("text",)
    assert verify_evidence_index(first.index_dir, corpus_dir=corpus).ok


def test_rights_and_access_filters_are_preserved(tmp_path):
    corpus = tmp_path / "corpus"
    record = write_corpus_item(
        CorpusItem(
            platform="blog",
            source_url="https://example.com/private",
            title="Licensed memo",
            body="Controlled launch strategy.",
        ),
        corpus,
        scraped_at=NOW.isoformat(),
    )
    assert record
    text = record.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    front = yaml.safe_load(parts[1])
    front["access_class"] = "licensed"
    front["rights"] = {
        "status": "licensed",
        "permitted_uses": ["local-research", "internal-search"],
        "license": "Example-License",
    }
    record.write_text(
        "---\n" + yaml.safe_dump(front, sort_keys=False) + "---" + parts[2], encoding="utf-8"
    )
    index = build_evidence_index(corpus, tmp_path / "index", now=NOW)
    denied = query_evidence_index(index.index_dir, text="launch", access_classes={"public"})
    allowed = query_evidence_index(index.index_dir, text="launch", access_classes={"licensed"})
    assert denied.citations == ()
    assert allowed.citations[0].rights.license == "Example-License"
    assert allowed.citations[0].rights.permitted_uses == (
        "local-research",
        "internal-search",
    )


def test_declared_image_asset_supports_local_visual_query(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    corpus = tmp_path / "corpus"
    assets_dir = corpus / "assets"
    assets_dir.mkdir(parents=True)
    image_path = assets_dir / "signal.png"
    Image.new("RGB", (32, 24), (20, 80, 220)).save(image_path)
    data = image_path.read_bytes()
    provenance = corpus / "_provenance"
    provenance.mkdir()
    row = {
        "schema_version": "provenance-asset.v1",
        "asset_id": "asset-blue-signal",
        "record_path": "assets/signal.png",
        "local_path": "assets/signal.png",
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "mime": "image/png",
        "modality": "image",
        "title": "Blue signal",
        "source_url": "https://example.com/board",
        "access_class": "owned",
        "rights": {"status": "owned", "permitted_uses": ["local-research"]},
        "metadata": {"alt": "blue launch signal"},
    }
    (provenance / "assets.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    index = build_evidence_index(corpus, tmp_path / "index", now=NOW, strict=True)
    assert index.visual_descriptors
    result = query_evidence_index(index.index_dir, image_path=image_path)
    citation = result.citations[0]
    assert citation.asset_id == "asset-blue-signal"
    assert citation.matched_modalities == ("visual",)
    assert citation.original_path == "assets/signal.png"
    assert citation.access_class == "owned"
    assert verify_evidence_index(index.index_dir, corpus_dir=corpus).ok


def test_text_and_visual_rankings_are_fused(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    corpus = tmp_path / "corpus"
    assets_dir = corpus / "assets"
    assets_dir.mkdir(parents=True)
    image_path = assets_dir / "shared.png"
    Image.new("RGB", (16, 16), (200, 30, 60)).save(image_path)
    data = image_path.read_bytes()
    provenance = corpus / "_provenance"
    provenance.mkdir()
    row = {
        "asset_id": "asset-shared",
        "record_path": "assets/shared.png",
        "local_path": "assets/shared.png",
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "mime": "image/png",
        "modality": "image",
        "title": "Launch mark",
        "metadata": {"caption": "crimson launch mark"},
    }
    (provenance / "assets.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    index = build_evidence_index(corpus, tmp_path / "index", now=NOW)
    result = query_evidence_index(index.index_dir, text="launch", image_path=image_path)
    assert result.citations[0].matched_modalities == ("text", "visual")
    assert result.citations[0].text_score is not None
    assert result.citations[0].visual_score is not None


def test_tombstoned_assets_are_not_returned(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    corpus = tmp_path / "corpus"
    asset = corpus / "gone.png"
    corpus.mkdir()
    Image.new("RGB", (8, 8), "black").save(asset)
    data = asset.read_bytes()
    provenance = corpus / "_provenance"
    provenance.mkdir()
    row = {
        "asset_id": "gone",
        "local_path": "gone.png",
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "modality": "image",
        "metadata": {"alt": "retired artifact"},
        "tombstoned_at": NOW.isoformat(),
    }
    (provenance / "assets.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    index = build_evidence_index(corpus, tmp_path / "index", now=NOW)
    result = query_evidence_index(index.index_dir, text="retired")
    assert result.citations == ()


def test_portable_asset_store_manifest_is_indexed_without_copying_bytes(tmp_path):
    corpus = tmp_path / "corpus"
    policy = SourcePolicy(
        RightsDeclaration("public", ("local-research",)),
        AuthorizationDeclaration("public"),
        "public",
        local_only=False,
    )
    asset = AssetStore(corpus).put(
        b"portable provenance statement",
        source_url="https://example.com/export?utm_source=test",
        media_type="text/plain",
        source_policy=policy,
        role="text",
        alt="Imported statement",
    )
    write_asset_manifest(corpus / "assets.json", [asset])
    index = build_evidence_index(corpus, tmp_path / "index", now=NOW, strict=True)
    result = query_evidence_index(index.index_dir, text="provenance statement")
    assert result.citations[0].asset_id == asset.asset_id
    assert result.citations[0].original_path == asset.relative_path
    assert result.citations[0].safe_source_url == "https://example.com/export"
    assert result.citations[0].rights.status == "public"
    assert result.citations[0].authorization == AuthorizationDeclaration("public")
    assert result.citations[0].local_only is False
    assert verify_evidence_index(index.index_dir, corpus_dir=corpus).ok


def test_atomic_current_pointer_selects_an_immutable_generation(tmp_path):
    corpus = tmp_path / "corpus"
    write_corpus_item(
        CorpusItem(
            platform="blog",
            source_url="https://example.com",
            title="Pointer",
            body="generation pointer",
        ),
        corpus,
        scraped_at=NOW.isoformat(),
    )
    root = tmp_path / "index"
    built = build_evidence_index(corpus, root, now=NOW)
    pointer = json.loads((root / "current.json").read_text())
    assert pointer["index_id"] == built.index_id
    assert built.index_dir == root / pointer["generation"]
    assert query_evidence_index(root, text="pointer").citations
    assert verify_evidence_index(root, corpus_dir=corpus).ok
    repeated = build_evidence_index(corpus, root, now="2026-07-15T00:00:00+00:00")
    assert repeated.index_dir == built.index_dir
    assert json.loads((root / "current.json").read_text())["index_id"] == built.index_id


def test_tampering_and_symlink_escape_are_detected(tmp_path):
    corpus = tmp_path / "corpus"
    record = write_corpus_item(
        CorpusItem(platform="blog", source_url="https://example.com", title="One", body="alpha"),
        corpus,
        scraped_at=NOW.isoformat(),
    )
    assert record
    index = build_evidence_index(corpus, tmp_path / "index", now=NOW)
    record.write_text(record.read_text() + "tampered", encoding="utf-8")
    result = verify_evidence_index(index.index_dir, corpus_dir=corpus)
    assert not result.ok
    assert any("changed" in error for error in result.errors)
    record.unlink()

    other = tmp_path / "outside.md"
    other.write_text("outside", encoding="utf-8")
    link = corpus / "linked.md"
    link.symlink_to(other)
    with pytest.raises(ValueError, match="contained regular file"):
        build_evidence_index(corpus, tmp_path / "strict-index", now=NOW, strict=True)


def test_malformed_asset_manifest_fails_closed_in_strict_mode(tmp_path):
    corpus = tmp_path / "corpus"
    provenance = corpus / "_provenance"
    provenance.mkdir(parents=True)
    (provenance / "assets.jsonl").write_text(
        json.dumps(
            {
                "asset_id": "escape",
                "local_path": "../secret.png",
                "sha256": "0" * 64,
                "bytes": 0,
                "modality": "image",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="relative to the corpus"):
        build_evidence_index(corpus, tmp_path / "index", now=NOW, strict=True)


def test_text_index_tampering_is_detected(tmp_path):
    corpus = tmp_path / "corpus"
    write_corpus_item(
        CorpusItem(
            platform="blog",
            source_url="https://example.com",
            title="One",
            body="alpha beta",
        ),
        corpus,
        scraped_at=NOW.isoformat(),
    )
    index = build_evidence_index(corpus, tmp_path / "index", now=NOW)
    text_index_path = index.index_dir / "text-index.json"
    text_index = json.loads(text_index_path.read_text())
    text_index["documents"] = []
    text_index_path.write_text(json.dumps(text_index), encoding="utf-8")
    result = verify_evidence_index(index.index_dir, corpus_dir=corpus)
    assert not result.ok
    assert "text index does not match evidence records" in result.errors


def test_resealed_text_and_locator_forgery_is_detected_against_source(tmp_path):
    corpus = tmp_path / "corpus"
    write_corpus_item(
        CorpusItem(
            platform="blog",
            source_url="https://example.com",
            title="Trusted",
            body="trusted source evidence",
        ),
        corpus,
        scraped_at=NOW.isoformat(),
    )
    index = build_evidence_index(corpus, tmp_path / "index", now=NOW)
    evidence_path = index.index_dir / "evidence.jsonl"
    rows = _jsonl(evidence_path)
    rows[0]["evidence_id"] = "evidence-forged"
    rows[0]["text"] = "forged text absent from the source"
    rows[0]["locator"] = {"char_start": 9999, "char_end": 10033}
    rows[0]["content_hash"] = hashlib.sha256(rows[0]["text"].encode()).hexdigest()
    _write_jsonl(evidence_path, rows)
    _reseal_index(index.index_dir)

    result = verify_evidence_index(index.index_dir, corpus_dir=corpus)

    assert not result.ok
    assert "indexed text evidence does not match corpus source bytes and locators" in result.errors


def test_resealed_policy_forgery_is_detected_against_source_manifest(tmp_path):
    corpus = tmp_path / "corpus"
    policy = SourcePolicy(
        RightsDeclaration("public", ("local-research",)),
        AuthorizationDeclaration("public"),
        "public",
        local_only=False,
    )
    asset = AssetStore(corpus).put(
        b"portable policy evidence",
        source_url="https://example.com/export",
        media_type="text/plain",
        source_policy=policy,
        role="text",
        alt="Policy evidence",
    )
    write_asset_manifest(corpus / "assets.json", [asset])
    index = build_evidence_index(corpus, tmp_path / "index", now=NOW, strict=True)
    assets_path = index.index_dir / "assets.jsonl"
    rows = _jsonl(assets_path)
    rows[0]["access_class"] = "owned"
    rows[0]["local_only"] = True
    rows[0]["rights"] = {"status": "owned", "permitted_uses": ["commercial"]}
    rows[0]["authorization"] = {"basis": "owned"}
    _write_jsonl(assets_path, rows)
    _reseal_index(index.index_dir)

    result = verify_evidence_index(index.index_dir, corpus_dir=corpus)

    assert not result.ok
    assert "indexed asset provenance and policy do not match corpus records" in result.errors


def test_query_rejects_an_internally_tampered_index(tmp_path):
    corpus = tmp_path / "corpus"
    write_corpus_item(
        CorpusItem(
            platform="blog",
            source_url="https://example.com",
            title="Trusted",
            body="trusted citation body",
        ),
        corpus,
        scraped_at=NOW.isoformat(),
    )
    index = build_evidence_index(corpus, tmp_path / "index", now=NOW)
    evidence_path = index.index_dir / "evidence.jsonl"
    rows = [json.loads(line) for line in evidence_path.read_text().splitlines()]
    rows[0]["text"] = "forged citation body"
    evidence_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError, match="text index does not match"):
        query_evidence_index(index.index_dir, text="trusted")


def test_visual_tampering_cannot_be_hidden_by_rewriting_the_manifest(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    corpus = tmp_path / "corpus"
    asset_path = corpus / "signal.png"
    corpus.mkdir()
    Image.new("RGB", (16, 16), (80, 120, 200)).save(asset_path)
    body = asset_path.read_bytes()
    provenance = corpus / "_provenance"
    provenance.mkdir()
    row = {
        "asset_id": "signal",
        "local_path": "signal.png",
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
        "modality": "image",
    }
    (provenance / "assets.jsonl").write_text(json.dumps(row) + "\n")
    index = build_evidence_index(corpus, tmp_path / "index", now=NOW)
    descriptor_path = index.index_dir / "visual-index.jsonl"
    descriptors = [json.loads(line) for line in descriptor_path.read_text().splitlines()]
    descriptors[0]["dhash"] = "0" * 16
    descriptors[0]["color_grid"] = [0] * 48
    descriptor_path.write_text(
        "".join(json.dumps(value, sort_keys=True) + "\n" for value in descriptors)
    )

    assets = [
        json.loads(line) for line in (index.index_dir / "assets.jsonl").read_text().splitlines()
    ]
    evidence = [
        json.loads(line) for line in (index.index_dir / "evidence.jsonl").read_text().splitlines()
    ]
    manifest_path = index.index_dir / "index-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    content = {
        "assets": assets,
        "evidence": evidence,
        "derivatives": [],
        "providers": manifest["providers"],
        "visual": descriptors,
    }
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest["content_hash"] = digest
    manifest["index_id"] = f"evidence-{digest[:20]}"
    manifest_path.write_text(json.dumps(manifest))
    result = verify_evidence_index(index.index_dir, corpus_dir=corpus)
    assert not result.ok
    assert any("visual descriptor" in error for error in result.errors)


def test_query_requires_input_and_positive_limits(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    index = build_evidence_index(corpus, tmp_path / "index", now=NOW)
    with pytest.raises(ValueError, match="requires text or image_path"):
        query_evidence_index(index.index_dir)
    with pytest.raises(ValueError, match="positive"):
        query_evidence_index(index.index_dir, text="x", limit=0)
