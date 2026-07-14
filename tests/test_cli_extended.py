import json
from pathlib import Path

from harness.base import CorpusItem, write_corpus_item
from harness.cli import main


def _write_public_policy(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "rights": {"status": "public", "permitted_uses": ["local-research"]},
                "authorization": {"basis": "public"},
                "access_class": "public",
                "local_only": False,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_source_gate_cli_is_machine_readable(capsys):
    assert main(["source", "check", "https://example.com/article"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "allow_public"

    assert main(["source", "check", "https://x.com/example"]) == 3
    value = json.loads(capsys.readouterr().out)
    assert value["action"] == "export_required"
    assert "packet" in value


def test_package_discover_validate_import_verify_cli(tmp_path: Path, capsys):
    source = tmp_path / "export"
    source.mkdir()
    (source / "note.md").write_text("operator-owned export", encoding="utf-8")
    policy = _write_public_policy(tmp_path / "policy.json")
    manifest = source / "source-package.json"
    imported = tmp_path / "imported"

    assert (
        main(
            [
                "package",
                "discover",
                str(source),
                "--manifest",
                str(manifest),
                "--package-id",
                "example-export",
                "--policy",
                str(policy),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["package", "validate", str(manifest)]) == 0
    assert json.loads(capsys.readouterr().out)["contract"] == "provenance-source-package.v1"
    assert main(["package", "import", str(manifest), "--out", str(imported)]) == 0
    capsys.readouterr()
    assert main(["package", "verify", str(imported)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_index_build_query_verify_cli(tmp_path: Path, capsys):
    corpus = tmp_path / "corpus"
    index = tmp_path / "index"
    write_corpus_item(
        CorpusItem(
            platform="blog",
            source_url="https://example.com/provenance",
            title="Lineage",
            body="Durable evidence keeps exact source lineage.",
        ),
        corpus,
        scraped_at="2026-07-14T00:00:00+00:00",
    )

    assert main(["index", "build", "--corpus", str(corpus), "--out", str(index)]) == 0
    capsys.readouterr()
    assert main(["index", "query", str(index), "--text", "source lineage"]) == 0
    assert json.loads(capsys.readouterr().out)["citations"]
    assert main(["index", "verify", str(index), "--corpus", str(corpus)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_media_default_performs_no_download(tmp_path: Path, capsys):
    policy = _write_public_policy(tmp_path / "policy.json")
    assert (
        main(
            [
                "media",
                "https://example.com/image.png",
                "--out",
                str(tmp_path / "assets"),
                "--policy",
                str(policy),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "not_downloaded"


def test_browser_screenshot_requires_rights_policy(tmp_path: Path, capsys):
    assert (
        main(
            [
                "browser",
                "https://example.com",
                "--out",
                str(tmp_path),
                "--screenshot",
            ]
        )
        == 2
    )
    assert "requires --policy" in capsys.readouterr().err
