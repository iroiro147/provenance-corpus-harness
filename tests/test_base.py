from datetime import datetime
from pathlib import Path

import pytest

from harness.base import BaseScraper, CorpusItem, html_to_text, slugify, write_corpus_item


def test_html_to_text_strips_tags_and_unescapes():
    out = html_to_text("<p>Hello&nbsp;<b>world</b> &amp; co<script>x=1</script></p>")
    assert "Hello" in out and "world" in out and "& co" in out
    assert "<" not in out and "script" not in out and "x=1" not in out


def test_slugify():
    assert slugify("Hello, World! 2026") == "hello-world-2026"
    assert slugify("") == "item"
    assert len(slugify("x" * 200)) <= 80


def test_write_corpus_item_roundtrip(tmp_path: Path):
    item = CorpusItem(
        platform="hackernews",
        source_url="https://news.ycombinator.com/item?id=1",
        title="Story One",
        author="alice",
        date="2023-11-14",
        body="The body prose.",
        extra={"score": 42},
    )
    p = write_corpus_item(item, tmp_path, scraped_at="2026-06-07T00:00:00Z")
    assert p is not None and p.exists()
    assert p == tmp_path / "hackernews" / "story-one.md"
    text = p.read_text()
    assert text.startswith("---\n")
    assert "platform: hackernews" in text
    assert "content_hash:" in text
    assert "# Story One" in text
    assert "The body prose." in text


def _frontmatter(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text().split("---", 2)[1])


def _parsed_timestamp(path: Path) -> datetime:
    value = _frontmatter(path)["scraped_at"]
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_write_defaults_to_timezone_aware_scraped_at(tmp_path: Path):
    item = CorpusItem(platform="x", source_url="https://example.com", body="content")

    path = write_corpus_item(item, tmp_path)

    assert path is not None
    scraped_at = _parsed_timestamp(path)
    assert scraped_at.tzinfo is not None
    assert scraped_at.utcoffset() is not None


@pytest.mark.parametrize("scraped_at", ["not-a-timestamp", "2026-07-13T12:00:00"])
def test_write_rejects_invalid_or_timezone_naive_scraped_at(tmp_path: Path, scraped_at: str):
    item = CorpusItem(platform="x", source_url="https://example.com", body="content")

    with pytest.raises(ValueError, match="scraped_at"):
        write_corpus_item(item, tmp_path, scraped_at=scraped_at)

    assert not tmp_path.joinpath("x").exists()


def test_base_scraper_run_uses_one_timestamp_for_the_batch(tmp_path: Path):
    class ExampleScraper(BaseScraper):
        def scrape(self, target: str, limit: int = 25):
            yield CorpusItem(platform="x", source_url="u1", title="one", body="first")
            yield CorpusItem(platform="x", source_url="u2", title="two", body="second")

    paths = ExampleScraper().run("unused", tmp_path)

    assert len(paths) == 2
    timestamps = {_parsed_timestamp(path) for path in paths}
    assert len(timestamps) == 1
    timestamp = timestamps.pop()
    assert timestamp.tzinfo is not None
    assert timestamp.utcoffset() is not None


def test_write_skips_empty_body(tmp_path: Path):
    assert (
        write_corpus_item(CorpusItem(platform="x", source_url="u", title="t", body="   "), tmp_path)
        is None
    )


def test_write_dedups_identical_and_disambiguates_collisions(tmp_path: Path):
    a = CorpusItem(platform="p", source_url="u1", title="Same Title", body="content A")
    b = CorpusItem(
        platform="p", source_url="u2", title="Same Title", body="content A"
    )  # identical body
    c = CorpusItem(platform="p", source_url="u3", title="Same Title", body="DIFFERENT content")

    p1 = write_corpus_item(a, tmp_path)
    p2 = write_corpus_item(b, tmp_path)  # identical hash → skipped
    p3 = write_corpus_item(c, tmp_path)  # same slug, different content → suffixed

    assert p1 is not None
    assert p2 is None  # dedup
    assert p3 is not None and p3 != p1  # disambiguated
    assert p3.name.startswith("same-title-")


@pytest.mark.parametrize(
    "platform",
    ["", ".", "..", "../../outside", "/tmp/outside", "nested/path", "GitHub", "has space"],
)
def test_write_rejects_unsafe_platform_identifiers(tmp_path: Path, platform: str):
    item = CorpusItem(platform=platform, source_url="https://example.com", body="content")

    with pytest.raises(ValueError, match="platform"):
        write_corpus_item(item, tmp_path)


def test_write_rejects_symlink_escape(tmp_path: Path):
    output_root = tmp_path / "output"
    outside = tmp_path / "outside"
    output_root.mkdir()
    outside.mkdir()
    (output_root / "github").symlink_to(outside, target_is_directory=True)
    item = CorpusItem(platform="github", source_url="https://example.com", body="content")

    with pytest.raises(ValueError, match="escapes"):
        write_corpus_item(item, output_root)

    assert list(outside.iterdir()) == []
