import pytest

from harness.scrapers.youtube import YouTubeScraper, vtt_to_text

VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000
Hello and welcome

00:00:02.000 --> 00:00:04.000
Hello and welcome
to the show

00:00:04.000 --> 00:00:06.000
to the show
[Music]

00:00:06.000 --> 00:00:08.000
<c>today we discuss APIs</c>
"""

INFO = {
    "id": "abc123XYZ_0",
    "title": "Designing REST APIs",
    "channel": "DevChannel",
    "upload_date": "20260601",
    "webpage_url": "https://www.youtube.com/watch?v=abc123XYZ_0",
    "description": "An intro to API design.",
    "duration": 600,
    "view_count": 12345,
    "channel_id": "UC_xyz",
}


def test_vtt_to_text_dedups_and_strips():
    out = vtt_to_text(VTT)
    assert "Hello and welcome" in out
    assert "to the show" in out
    assert "today we discuss APIs" in out
    assert "[Music]" not in out  # bracket cues removed
    assert "-->" not in out and "WEBVTT" not in out
    # rolling-caption duplicate collapsed (not "Hello and welcome Hello and welcome")
    assert out.count("Hello and welcome") == 1


def test_fetch_video_offline_combines_description_and_transcript():
    yt = YouTubeScraper(run=lambda args: (0, "", ""))  # runner never used (we inject data)
    item = yt.fetch_video("abc123XYZ_0", info_json=INFO, vtt_text=VTT)

    assert item is not None
    assert item.platform == "youtube"
    assert item.title == "Designing REST APIs"
    assert item.author == "DevChannel"
    assert item.date == "20260601"
    assert item.source_url == "https://www.youtube.com/watch?v=abc123XYZ_0"
    assert "An intro to API design." in item.body
    assert "## Transcript" in item.body
    assert "today we discuss APIs" in item.body
    assert item.extra["video_id"] == "abc123XYZ_0"
    assert item.extra["has_transcript"] is True


def test_fetch_video_returns_none_on_dump_failure():
    yt = YouTubeScraper(run=lambda args: (1, "", "error"))
    assert yt.fetch_video("abc123XYZ_0") is None


@pytest.mark.parametrize(
    "target",
    [
        "https://example.com/video",
        "https://user:pass@youtube.com/watch?v=abc123XYZ_0",
        "https://youtube.com/watch?v=abc123XYZ_0&token=secret",
    ],
)
def test_live_youtube_target_is_constrained_before_runner(target):
    called = False

    def runner(args):
        nonlocal called
        called = True
        return 1, "", ""

    with pytest.raises(ValueError):
        YouTubeScraper(run=runner).fetch_video(target)
    assert not called
