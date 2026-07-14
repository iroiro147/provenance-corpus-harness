import pytest

from harness import __version__
from harness.base import BaseScraper, CorpusItem
from harness.cli import main


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"corpus-harness {__version__}"


def test_help_uses_standalone_product_language(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "provenance-rich Markdown" in help_text
    assert "corpus-harness" in help_text


def test_cli_redacts_target_and_writes_receipt(monkeypatch, tmp_path, capsys):
    class FakeScraper(BaseScraper):
        platform = "fake"

        def scrape(self, target: str, limit: int = 25):
            yield CorpusItem(platform="fake", source_url="https://example.com", body="body")

    monkeypatch.setattr("harness.cli.build_scraper", lambda *args: FakeScraper())
    code = main(["rss", "https://example.com/feed?token=SUPERSECRET", "--out", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 2
    assert "SUPERSECRET" not in captured.err


def test_cli_resume_refresh_are_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["rss", "https://example.com/feed", "--out", str(tmp_path), "--resume", "--refresh"])
    assert exc.value.code == 2


def test_cli_success_empty_failure_and_resume(monkeypatch, tmp_path, capsys):
    class FakeScraper(BaseScraper):
        platform = "fake"

        def __init__(self):
            self.calls = 0
            self.mode = "written"

        def scrape(self, target: str, limit: int = 25):
            self.calls += 1
            if self.mode == "failed":
                raise RuntimeError("owned failure")
            if self.mode == "written":
                yield CorpusItem(platform="fake", source_url="https://example.com", body="body")

    scraper = FakeScraper()
    monkeypatch.setattr("harness.cli.build_scraper", lambda *args: scraper)

    assert main(["rss", "https://example.com/feed", "--out", str(tmp_path)]) == 0
    assert main(["rss", "https://example.com/feed", "--out", str(tmp_path), "--resume"]) == 0
    assert scraper.calls == 1

    scraper.mode = "empty"
    assert main(["rss", "https://example.com/other", "--out", str(tmp_path)]) == 0
    scraper.mode = "failed"
    assert main(["rss", "https://example.com/fail", "--out", str(tmp_path)]) == 1
    assert "owned failure" in capsys.readouterr().err
