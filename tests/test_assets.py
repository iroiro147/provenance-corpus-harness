import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from harness.assets import AssetStore, merge_asset_manifest, write_asset_manifest
from harness.rights import AuthorizationDeclaration, RightsDeclaration, SourcePolicy


def public_policy() -> SourcePolicy:
    return SourcePolicy(RightsDeclaration("public"), AuthorizationDeclaration("public"), "public")


def test_assets_are_content_addressed_and_deduplicated(tmp_path: Path) -> None:
    store = AssetStore(tmp_path)
    first = store.put(
        b"image",
        source_url="https://example.com/a.png",
        media_type="image/png",
        source_policy=public_policy(),
    )
    second = store.put(
        b"image",
        source_url="https://example.com/b.png",
        media_type="image/png",
        source_policy=public_policy(),
    )
    digest = hashlib.sha256(b"image").hexdigest()
    assert first.relative_path == f"_assets/sha256/{digest[:2]}/{digest}"
    assert second.relative_path == first.relative_path
    assert first.blob_id == second.blob_id
    assert first.asset_id != second.asset_id
    assert store.verify(first)


def test_asset_caps_and_declared_integrity_are_enforced(tmp_path: Path) -> None:
    store = AssetStore(tmp_path, max_asset_bytes=3, max_total_bytes=4, max_assets=2)
    with pytest.raises(ValueError, match="exceeds 3"):
        store.put(b"four", source_url="", media_type="image/png", source_policy=public_policy())
    with pytest.raises(ValueError, match="sha256"):
        store.put(
            b"ok",
            source_url="",
            media_type="image/png",
            source_policy=public_policy(),
            expected_sha256="0" * 64,
        )


def test_asset_verification_detects_corruption(tmp_path: Path) -> None:
    store = AssetStore(tmp_path)
    asset = store.put(
        b"safe", source_url="", media_type="application/pdf", source_policy=public_policy()
    )
    (tmp_path / asset.relative_path).write_bytes(b"changed")
    assert not store.verify(asset)


def test_concurrent_publication_produces_one_valid_file(tmp_path: Path) -> None:
    def publish(_: int):
        return AssetStore(tmp_path).put(
            b"shared", source_url="", media_type="image/png", source_policy=public_policy()
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        assets = list(pool.map(publish, range(20)))
    assert len({asset.relative_path for asset in assets}) == 1
    assert (tmp_path / assets[0].relative_path).read_bytes() == b"shared"


def test_asset_manifest_is_atomic_and_sanitized(tmp_path: Path) -> None:
    asset = AssetStore(tmp_path).put(
        b"x",
        source_url="https://example.com/x?utm_source=test",
        media_type="image/png",
        source_policy=public_policy(),
        alt="caption",
    )
    path = write_asset_manifest(tmp_path / "run" / "assets.json", [asset])
    payload = json.loads(path.read_text())
    assert payload["contract"] == "provenance-assets.v1"
    assert payload["assets"][0]["sha256"] == asset.sha256


def test_asset_manifest_merge_preserves_prior_runs(tmp_path: Path) -> None:
    first = AssetStore(tmp_path).put(
        b"one",
        source_url="https://example.com/one",
        media_type="text/plain",
        source_policy=public_policy(),
    )
    second = AssetStore(tmp_path).put(
        b"two",
        source_url="https://example.com/two",
        media_type="text/plain",
        source_policy=public_policy(),
    )
    path = merge_asset_manifest(tmp_path / "assets.json", [first])
    merge_asset_manifest(path, [second, first])

    payload = json.loads(path.read_text())
    assert [row["asset_id"] for row in payload["assets"]] == sorted(
        [first.asset_id, second.asset_id]
    )


def test_asset_store_rejects_symlinked_storage(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "_assets").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        AssetStore(tmp_path).put(
            b"x", source_url="", media_type="image/png", source_policy=public_policy()
        )
