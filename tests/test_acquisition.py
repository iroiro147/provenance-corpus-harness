import json
from pathlib import Path

import pytest

from harness.acquisition import _write_receipt, collect
from harness.base import BaseScraper, CorpusItem


class CountingScraper(BaseScraper):
    platform = "test"

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def scrape(self, target: str, limit: int = 25):
        self.calls += 1
        yield CorpusItem(platform="test", source_url="https://example.com/one", body="one")
        yield CorpusItem(platform="test", source_url="https://example.com/empty", body="")
        if self.fail:
            raise RuntimeError("failed at https://example.com/x?token=secret")


def test_receipt_is_deterministic_relative_and_resumable(tmp_path: Path):
    scraper = CountingScraper()
    first = collect(scraper, "https://example.com/feed?utm_source=x", tmp_path, limit=3)

    assert first.status == "complete"
    assert first.written == 1 and first.empty == 1 and first.failed == 0
    payload = json.loads(first.receipt_path.read_text())
    assert payload["paths"] == ["test/https-example-com-one.md"]
    assert payload["records"][0]["path"] == payload["paths"][0]
    assert len(payload["records"][0]["record_hash"]) == 64
    assert str(tmp_path) not in first.receipt_path.read_text()
    assert payload["spec"]["target"] == "https://example.com/feed?utm_source=x"

    reused = collect(
        scraper, "https://example.com/feed?utm_source=x", tmp_path, limit=3, resume=True
    )
    assert reused.status == "reused"
    assert reused.fingerprint == first.fingerprint
    assert scraper.calls == 1


def test_resume_refetches_when_record_was_tampered(tmp_path: Path):
    scraper = CountingScraper()
    first = collect(scraper, "target", tmp_path)
    first.paths[0].write_text(first.paths[0].read_text() + "tampered\n")

    result = collect(scraper, "target", tmp_path, resume=True)

    assert result.status == "complete"
    assert scraper.calls == 2


def test_duplicate_rerun_preserves_record_for_later_resume(tmp_path: Path):
    scraper = CountingScraper()
    first = collect(scraper, "target", tmp_path)
    second = collect(scraper, "target", tmp_path)
    resumed = collect(scraper, "target", tmp_path, resume=True)

    assert second.written == 0 and second.duplicates == 1
    assert second.paths == first.paths
    assert resumed.status == "reused" and resumed.paths == first.paths
    assert scraper.calls == 2


def test_refresh_runs_again_and_changed_spec_does_not_reuse(tmp_path: Path):
    scraper = CountingScraper()
    collect(scraper, "target", tmp_path, limit=1)
    collect(scraper, "target", tmp_path, limit=1, refresh=True)
    changed = collect(scraper, "target", tmp_path, limit=2, resume=True)
    assert scraper.calls == 3
    assert changed.status == "complete"


def test_partial_receipt_preserves_paths_and_redacts_error(tmp_path: Path):
    result = collect(CountingScraper(fail=True), "target", tmp_path)
    payload = json.loads(result.receipt_path.read_text())
    assert result.status == "partial"
    assert result.written == 1 and result.failed == 1
    assert payload["paths"] == ["test/https-example-com-one.md"]
    assert "secret" not in result.receipt_path.read_text()
    assert "token" not in result.error


def test_receipt_target_does_not_persist_url_credentials(tmp_path: Path):
    with pytest.raises(ValueError, match="credentials"):
        collect(
            CountingScraper(),
            "https://user:pass@example.com/x?api_key=secret&ok=yes",
            tmp_path,
        )
    assert not tmp_path.joinpath("_provenance").exists()


def test_non_url_target_secret_is_redacted(tmp_path: Path):
    result = collect(CountingScraper(), "owner/repo?token=secret", tmp_path)
    assert "secret" not in result.receipt_path.read_text()


def test_adapter_options_change_fingerprint(tmp_path: Path):
    class Configured(CountingScraper):
        def __init__(self, mode):
            super().__init__()
            self.mode = mode

        def acquisition_options(self):
            return {**super().acquisition_options(), "mode": self.mode}

    one = collect(Configured("one"), "target", tmp_path)
    two = collect(Configured("two"), "target", tmp_path, resume=True)
    assert one.fingerprint != two.fingerprint


def test_provenance_symlink_escape_is_rejected(tmp_path: Path):
    outside = tmp_path / "outside"
    root = tmp_path / "root"
    outside.mkdir()
    root.mkdir()
    (root / "_provenance").symlink_to(outside, target_is_directory=True)

    try:
        collect(CountingScraper(), "target", root)
    except ValueError as exc:
        assert "symlink" in str(exc)
    else:
        raise AssertionError("expected provenance symlink rejection")


def test_receipt_replace_does_not_follow_destination_symlink(tmp_path: Path):
    outside = tmp_path / "outside.json"
    outside.write_text("SAFE")
    receipt = tmp_path / "receipt.json"
    receipt.symlink_to(outside)

    _write_receipt(receipt, {"status": "complete"})

    assert outside.read_text() == "SAFE"
    assert not receipt.is_symlink()
    assert json.loads(receipt.read_text()) == {"status": "complete"}


def test_attempt_receipts_are_immutable_and_latest_is_an_index(tmp_path: Path):
    scraper = CountingScraper()
    first = collect(scraper, "target", tmp_path)
    second = collect(scraper, "target", tmp_path, refresh=True)

    assert first.receipt_path != second.receipt_path
    assert first.receipt_path.exists() and second.receipt_path.exists()
    latest = json.loads((second.receipt_path.parent / "latest.json").read_text())
    assert latest["run_id"] == second.run_id


def test_absolute_targets_and_error_paths_are_redacted(tmp_path: Path):
    class Failure(CountingScraper):
        def scrape(self, target: str, limit: int = 25):
            raise FileNotFoundError(f"missing {Path.home()}/private/input.xml")
            yield  # pragma: no cover

    result = collect(Failure(), "/Users/example/private/feed.xml", tmp_path)
    text = result.receipt_path.read_text()
    assert result.status == "failed"
    assert "/Users/" not in text
    assert "<local-path>" in text or "<home>" in text
