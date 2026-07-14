"""
YouTube scraper — transcript + metadata via a separately installed `yt-dlp` executable.
It deliberately avoids video download, FFmpeg frame extraction, and comment threading:
this adapter only produces a text record from metadata and an available transcript.

Target: a video URL or 11-char video id (comma-separated for several).
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import tempfile
from typing import Callable, Iterable
from urllib.parse import urlsplit

from ..base import BaseScraper, CorpusItem
from ..url_safety import assert_safe_public_url

# A runner is (args:list[str]) -> (returncode, stdout, stderr); injected in tests.
Runner = Callable[[list], "tuple[int, str, str]"]

_TS_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s*-->")
_TAG_RE = re.compile(r"<[^>]+>")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}


def _validated_youtube_target(value: str) -> str:
    if _VIDEO_ID_RE.fullmatch(value):
        return value
    safe = assert_safe_public_url(value)
    host = (urlsplit(safe).hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        raise ValueError("YouTube adapter accepts only video IDs and official YouTube URLs")
    return safe


def vtt_to_text(vtt: str) -> str:
    """Parse a WebVTT subtitle blob into clean, de-duplicated transcript prose."""
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        if _TS_RE.match(line) or "-->" in line:
            continue
        if line.isdigit():  # cue index
            continue
        line = _TAG_RE.sub("", line)  # inline <c> karaoke tags
        line = re.sub(r"\[[^\]]*\]", "", line).strip()  # [Music], [Applause]
        if not line:
            continue
        if lines and lines[-1] == line:  # auto-subs repeat each line across cues
            continue
        lines.append(line)
    # collapse adjacent duplicates that aren't strictly consecutive (rolling captions)
    out: list[str] = []
    for ln in lines:
        if not out or out[-1] != ln:
            out.append(ln)
    return " ".join(out).strip()


class YouTubeScraper(BaseScraper):
    platform = "youtube"

    def __init__(self, run: Runner | None = None, ytdlp_bin: str = "yt-dlp", sub_lang: str = "en"):
        # NB: store as `_runner`, NOT `self.run` — `run()` is BaseScraper's drive method.
        self._runner = run or self._default_run
        self.ytdlp_bin = ytdlp_bin
        self.sub_lang = sub_lang

    def acquisition_options(self) -> dict[str, object]:
        return {**super().acquisition_options(), "sub_lang": self.sub_lang}

    def _default_run(self, args: list) -> "tuple[int, str, str]":
        proc = subprocess.run(  # noqa: S603 — args is a fixed list, no shell
            [self.ytdlp_bin, *args], capture_output=True, text=True, timeout=180
        )
        return proc.returncode, proc.stdout, proc.stderr

    def scrape(self, target: str, limit: int = 5) -> Iterable[CorpusItem]:
        targets = [t.strip() for t in (target or "").split(",") if t.strip()][: max(limit, 1)]
        for t in targets:
            item = self.fetch_video(t)
            if item is not None:
                yield item

    def fetch_video(
        self, url_or_id: str, *, info_json: dict | None = None, vtt_text: str | None = None
    ) -> "CorpusItem | None":
        if info_json is None or vtt_text is None:
            url_or_id = _validated_youtube_target(url_or_id)
        if info_json is None:
            code, out, _ = self._runner(["--dump-json", "--skip-download", url_or_id])
            if code != 0 or not out.strip():
                return None
            try:
                info = json.loads(out.splitlines()[0])
            except Exception:  # noqa: BLE001
                return None
        else:
            info = info_json

        transcript = (
            vtt_to_text(vtt_text) if vtt_text is not None else self._fetch_transcript(url_or_id)
        )

        parts: list[str] = []
        desc = (info.get("description") or "").strip()
        if desc:
            parts.append(desc)
        if transcript:
            parts.append("## Transcript\n\n" + transcript)
        body = "\n\n".join(parts)
        if not body.strip():
            return None

        vid = info.get("id", url_or_id)
        return CorpusItem(
            platform=self.platform,
            source_url=info.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
            title=info.get("title", "") or "",
            author=info.get("channel") or info.get("uploader", "") or "",
            date=str(info.get("upload_date", "") or ""),
            body=body,
            extra={
                "video_id": vid,
                "duration": info.get("duration"),
                "view_count": info.get("view_count"),
                "channel_id": info.get("channel_id"),
                "has_transcript": bool(transcript),
            },
        )

    def _fetch_transcript(self, url_or_id: str) -> str:
        """Fetch auto/manual subtitles via yt-dlp into a temp dir, parse the VTT."""
        with tempfile.TemporaryDirectory() as td:
            out_tmpl = os.path.join(td, "%(id)s.%(ext)s")
            code, _, _ = self._runner(
                [
                    "--skip-download",
                    "--write-auto-sub",
                    "--write-sub",
                    "--sub-lang",
                    self.sub_lang,
                    "--sub-format",
                    "vtt",
                    "-o",
                    out_tmpl,
                    url_or_id,
                ]
            )
            vtts = glob.glob(os.path.join(td, "*.vtt"))
            if code != 0 and not vtts:
                return ""
            for path in vtts:
                try:
                    with open(path, encoding="utf-8", errors="ignore") as fh:
                        text = vtt_to_text(fh.read())
                    if text:
                        return text
                except Exception:  # noqa: BLE001
                    continue
        return ""
