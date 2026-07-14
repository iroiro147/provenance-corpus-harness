import hashlib
import json
from pathlib import Path

import pytest

from harness.evidence import build_evidence_index
from harness.rights import AuthorizationDeclaration, RightsDeclaration, SourcePolicy
from harness.source_package import (
    SourcePackageItem,
    SourcePackageManifest,
    assert_no_secrets,
    discover_source_package,
    import_source_package,
    load_source_package,
    verify_import,
)

CREATED_AT = "2026-07-14T00:00:00+00:00"


def owned_policy() -> SourcePolicy:
    return SourcePolicy(
        RightsDeclaration("owned"),
        AuthorizationDeclaration("account-owned-export", account_owner="operator"),
        "owned",
    )


def write_manifest(path: Path, manifest: SourcePackageManifest) -> None:
    path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")


def test_discover_hashes_and_imports_immutable_assets(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "note.md").write_text("source note", encoding="utf-8")
    (package / "image.png").write_bytes(b"png")
    (package / "source-metadata.jsonl").write_text(
        json.dumps(
            {
                "path": "image.png",
                "title": "Reference",
                "source_url": "https://example.com/image.png",
                "metadata": {"collection": "owned-export"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = discover_source_package(
        package,
        package_id="owned-export",
        connector="generic-export",
        source_policy=owned_policy(),
        created_at=CREATED_AT,
    )
    assert len(manifest.items) == 2
    assert all(len(item.sha256) == 64 for item in manifest.items)
    manifest_path = package / "source-package.json"
    write_manifest(manifest_path, manifest)

    result = import_source_package(manifest_path, tmp_path / "corpus")
    assert len(result.assets) == 2
    assert result.total_bytes == len(b"source note") + len(b"png")
    assert verify_import(result.output_dir).ok
    assert all((result.output_dir / asset.relative_path).is_file() for asset in result.assets)


def test_identical_blobs_keep_distinct_source_asset_identities(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "a.md").write_text("same", encoding="utf-8")
    (package / "b.md").write_text("same", encoding="utf-8")
    manifest = discover_source_package(
        package,
        package_id="same-bytes",
        connector="document-set",
        source_policy=owned_policy(),
        created_at=CREATED_AT,
    )
    path = package / "source-package.json"
    write_manifest(path, manifest)
    result = import_source_package(path, tmp_path / "corpus")
    assert len({asset.asset_id for asset in result.assets}) == 2
    assert len({asset.blob_id for asset in result.assets}) == 1
    assert len({asset.relative_path for asset in result.assets}) == 1
    assert verify_import(result.output_dir).ok
    index = build_evidence_index(result.output_dir, tmp_path / "index")
    assert len(index.assets) == 2


def test_import_detects_source_mutation_before_publication(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    source = package / "note.md"
    source.write_text("original", encoding="utf-8")
    manifest = discover_source_package(
        package,
        package_id="tamper",
        connector="document-set",
        source_policy=owned_policy(),
        created_at=CREATED_AT,
    )
    manifest_path = package / "source-package.json"
    write_manifest(manifest_path, manifest)
    source.write_text("changed", encoding="utf-8")
    output = tmp_path / "corpus"
    with pytest.raises(ValueError, match="does not match"):
        import_source_package(manifest_path, output)
    assert not output.exists()


def test_discovery_rejects_credential_material_inside_text_exports(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "messages.txt").write_text(
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="credential-like"):
        discover_source_package(
            package,
            package_id="secret-text",
            connector="document-set",
            source_policy=owned_policy(),
            created_at=CREATED_AT,
        )


def test_verify_detects_asset_corruption(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "note.md").write_text("original", encoding="utf-8")
    manifest = discover_source_package(
        package,
        package_id="verify",
        connector="document-set",
        source_policy=owned_policy(),
        created_at=CREATED_AT,
    )
    path = package / "source-package.json"
    write_manifest(path, manifest)
    result = import_source_package(path, tmp_path / "corpus")
    (result.output_dir / result.assets[0].relative_path).write_bytes(b"corrupt")
    verdict = verify_import(result.output_dir)
    assert not verdict.ok
    assert any("hash mismatch" in error for error in verdict.errors)


def test_verify_enforces_semantics_even_if_local_receipt_is_rehashed(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "note.md").write_text("original", encoding="utf-8")
    manifest = discover_source_package(
        package,
        package_id="semantic-verify",
        connector="document-set",
        source_policy=owned_policy(),
        created_at=CREATED_AT,
    )
    path = package / "source-package.json"
    write_manifest(path, manifest)
    result = import_source_package(path, tmp_path / "corpus")
    assets = json.loads(result.assets_path.read_text())
    assets["assets"][0]["asset_id"] = "asset-" + "0" * 64
    result.assets_path.write_text(json.dumps(assets), encoding="utf-8")
    receipt = json.loads(result.receipt_path.read_text())
    receipt["assets_sha256"] = hashlib.sha256(result.assets_path.read_bytes()).hexdigest()
    result.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    verdict = verify_import(result.output_dir)
    assert not verdict.ok
    assert any("provenance identity" in error for error in verdict.errors)


def test_verify_binds_asset_title_to_source_item(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "note.md").write_text("original", encoding="utf-8")
    manifest = discover_source_package(
        package,
        package_id="title-verify",
        connector="document-set",
        source_policy=owned_policy(),
        created_at=CREATED_AT,
    )
    path = package / "source-package.json"
    write_manifest(path, manifest)
    result = import_source_package(path, tmp_path / "corpus")
    assets = json.loads(result.assets_path.read_text())
    assets["assets"][0]["alt"] = "forged title"
    result.assets_path.write_text(json.dumps(assets), encoding="utf-8")
    receipt = json.loads(result.receipt_path.read_text())
    receipt["assets_sha256"] = hashlib.sha256(result.assets_path.read_bytes()).hexdigest()
    result.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    verdict = verify_import(result.output_dir)
    assert not verdict.ok
    assert any("does not match source item" in error for error in verdict.errors)


def test_discovery_rejects_symlinks_and_session_state(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    (package / "linked.md").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        discover_source_package(
            package,
            package_id="bad",
            connector="generic-export",
            source_policy=owned_policy(),
            created_at=CREATED_AT,
        )
    (package / "linked.md").unlink()
    cookies = package / "Cookies"
    cookies.mkdir()
    (cookies / "state.txt").write_text("state")
    with pytest.raises(ValueError, match="session state"):
        discover_source_package(
            package,
            package_id="bad",
            connector="generic-export",
            source_policy=owned_policy(),
            created_at=CREATED_AT,
        )


def test_package_root_manifest_and_output_symlinks_are_rejected(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "note.md").write_text("note")
    package_link = tmp_path / "package-link"
    package_link.symlink_to(package, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        discover_source_package(
            package_link,
            package_id="linked",
            connector="document-set",
            source_policy=owned_policy(),
            created_at=CREATED_AT,
        )
    manifest = discover_source_package(
        package,
        package_id="linked",
        connector="document-set",
        source_policy=owned_policy(),
        created_at=CREATED_AT,
    )
    real_manifest = package / "source-package.json"
    write_manifest(real_manifest, manifest)
    manifest_link = tmp_path / "manifest-link.json"
    manifest_link.symlink_to(real_manifest)
    with pytest.raises(ValueError, match="symlink"):
        load_source_package(manifest_link)
    output_target = tmp_path / "output-target"
    output_target.mkdir()
    output_link = tmp_path / "output-link"
    output_link.symlink_to(output_target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        import_source_package(real_manifest, output_link)


def test_discovery_is_idempotent_with_custom_manifest_name(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "note.md").write_text("note")
    first = discover_source_package(
        package,
        package_id="custom",
        connector="document-set",
        source_policy=owned_policy(),
        created_at=CREATED_AT,
    )
    write_manifest(package / "custom.json", first)
    second = discover_source_package(
        package,
        package_id="custom",
        connector="document-set",
        source_policy=owned_policy(),
        created_at=CREATED_AT,
    )
    assert [item.path for item in second.items] == ["note.md"]


@pytest.mark.parametrize(
    "value",
    [
        {"access_token": "value"},
        {"metadata": "Cookie: sid=value"},
        {"url": "https://example.com/path?api_key=value"},
        {"path": "Browser Profile/state"},
        {"auth": "Bearer abcdefghijklmnop"},
    ],
)
def test_secret_and_session_values_are_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        assert_no_secrets(value)


def test_manifest_rejects_duplicate_ids_and_paths() -> None:
    item = SourcePackageItem("same", "a.md", "text", "text/markdown", "A", "0" * 64, 0)
    duplicate_id = SourcePackageItem("same", "b.md", "text", "text/markdown", "B", "0" * 64, 0)
    with pytest.raises(ValueError, match="item_id"):
        SourcePackageManifest(
            "duplicate", "document-set", CREATED_AT, owned_policy(), (item, duplicate_id)
        )
    duplicate_path = SourcePackageItem("other", "a.md", "text", "text/markdown", "B", "0" * 64, 0)
    with pytest.raises(ValueError, match="paths"):
        SourcePackageManifest(
            "duplicate", "document-set", CREATED_AT, owned_policy(), (item, duplicate_path)
        )


def test_manifest_rejects_dangling_transcript_and_non_http_source() -> None:
    with pytest.raises(ValueError, match="transcript_path"):
        SourcePackageManifest(
            "transcript",
            "media-set",
            CREATED_AT,
            owned_policy(),
            (
                SourcePackageItem(
                    "clip",
                    "clip.mp4",
                    "video",
                    "video/mp4",
                    "Clip",
                    "0" * 64,
                    0,
                    transcript_path="clip.vtt",
                ),
            ),
        )
    with pytest.raises(ValueError, match="HTTP"):
        SourcePackageItem(
            "local",
            "a.md",
            "text",
            "text/markdown",
            "A",
            "0" * 64,
            0,
            source_url="file:///tmp/a.md",
        )


def test_import_enforces_item_and_total_limits(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "a.md").write_text("1234")
    manifest = discover_source_package(
        package,
        package_id="limits",
        connector="document-set",
        source_policy=owned_policy(),
        created_at=CREATED_AT,
    )
    path = package / "source-package.json"
    write_manifest(path, manifest)
    with pytest.raises(ValueError, match="exceeds 3"):
        import_source_package(path, tmp_path / "corpus", max_item_bytes=3)
    with pytest.raises(ValueError, match="limits must be positive"):
        import_source_package(path, tmp_path / "corpus", max_items=0)


def test_discovery_checks_size_before_reading_payload(tmp_path: Path, monkeypatch) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "large.md").write_bytes(b"1234")

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("unbounded Path.read_bytes must not be used")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read)
    with pytest.raises(ValueError, match="exceeds 3"):
        discover_source_package(
            package,
            package_id="bounded",
            connector="document-set",
            source_policy=owned_policy(),
            created_at=CREATED_AT,
            max_item_bytes=3,
        )


def test_load_rejects_manifest_over_budget(tmp_path: Path) -> None:
    path = tmp_path / "source-package.json"
    path.write_text("{}" * 20)
    with pytest.raises(ValueError, match="exceeds"):
        load_source_package(path, max_manifest_bytes=10)


def test_discovery_and_loading_share_exact_serialized_manifest_limit(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "note.md").write_text("note")
    manifest = discover_source_package(
        package,
        package_id="boundary",
        connector="document-set",
        source_policy=owned_policy(),
        created_at=CREATED_AT,
    )
    rendered = (json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n").encode()
    exact_limit = len(rendered)
    accepted = discover_source_package(
        package,
        package_id="boundary",
        connector="document-set",
        source_policy=owned_policy(),
        created_at=CREATED_AT,
        max_manifest_bytes=exact_limit,
    )
    path = package / "source-package.json"
    path.write_bytes(rendered)
    assert load_source_package(path, max_manifest_bytes=exact_limit) == accepted
    with pytest.raises(ValueError, match=f"exceeds {exact_limit - 1}"):
        discover_source_package(
            package,
            package_id="boundary",
            connector="document-set",
            source_policy=owned_policy(),
            created_at=CREATED_AT,
            max_manifest_bytes=exact_limit - 1,
        )
    with pytest.raises(ValueError, match=f"exceeds {exact_limit - 1}"):
        load_source_package(path, max_manifest_bytes=exact_limit - 1)
