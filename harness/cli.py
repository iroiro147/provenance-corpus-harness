"""Command-line interface for provenance-first Markdown corpus collection."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from . import __version__
from .acquisition import collect
from .scrapers import SCRAPERS
from .scrapers.blog import BlogScraper
from .scrapers.github import GitHubScraper
from .scrapers.hackernews import HackerNewsScraper
from .scrapers.producthunt import ProductHuntScraper
from .scrapers.reddit import RedditScraper
from .scrapers.rss import RSSScraper
from .scrapers.youtube import YouTubeScraper
from .url_safety import redact_sensitive_text


def _rss_platform_for(target: str) -> str:
    t = (target or "").lower()
    if "substack.com" in t:
        return "substack"
    if "medium.com" in t:
        return "medium"
    return "rss"


def build_scraper(platform: str, target: str, max_comments: int):
    if platform == "hackernews":
        return HackerNewsScraper(max_comments=max_comments)
    if platform == "rss":
        return RSSScraper(platform_name=_rss_platform_for(target))
    if platform == "blog":
        return BlogScraper()
    if platform == "youtube":
        return YouTubeScraper()
    if platform == "reddit":
        return RedditScraper(max_comments=max_comments)
    if platform == "producthunt":
        return ProductHuntScraper()
    if platform == "github":
        return GitHubScraper()
    raise SystemExit(f"unknown platform: {platform} (choices: {', '.join(sorted(SCRAPERS))})")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="corpus-harness",
        description="Collect operator-authorized content into provenance-rich Markdown records.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("platform", choices=sorted(SCRAPERS.keys()))
    p.add_argument(
        "target",
        help=(
            "hackernews: top|new|best|ask|show|<id>  ·  rss: feed URL  ·  "
            "blog: article URL(s)  ·  youtube: video URL/id(s)  ·  "
            "reddit: r/<sub>[/<sort>]  ·  producthunt: featured  ·  github: owner/repo(s)"
        ),
    )
    p.add_argument("--out", default="out", help="output corpus directory")
    p.add_argument("--limit", type=int, default=25, help="max items")
    p.add_argument(
        "--max-comments", type=int, default=10, help="hackernews: top comments per story"
    )
    run_mode = p.add_mutually_exclusive_group()
    run_mode.add_argument(
        "--resume", action="store_true", help="reuse a matching complete acquisition receipt"
    )
    run_mode.add_argument(
        "--refresh", action="store_true", help="run again even when a matching receipt exists"
    )
    args = p.parse_args(argv)

    scraped_at = datetime.now(timezone.utc).isoformat()
    scraper = build_scraper(args.platform, args.target, args.max_comments)
    print(
        f"[harness] {args.platform} ← {redact_sensitive_text(args.target)} "
        f"(limit {args.limit}) → {args.out}/",
        file=sys.stderr,
    )

    try:
        result = collect(
            scraper,
            args.target,
            args.out,
            limit=args.limit,
            resume=args.resume,
            refresh=args.refresh,
            scraped_at=scraped_at,
        )
    except (TypeError, ValueError) as exc:
        print(f"[harness] rejected: {redact_sensitive_text(str(exc))}", file=sys.stderr)
        return 2

    print(
        f"[harness] {result.status}: wrote={result.written} duplicate={result.duplicates} "
        f"empty={result.empty} failed={result.failed}",
        file=sys.stderr,
    )
    for pth in result.paths:
        print(f"  {pth}")
    print(f"[harness] receipt: {result.receipt_path}", file=sys.stderr)
    if not result.paths:
        print("[harness] nothing written (empty / robots-disallowed / duplicate).", file=sys.stderr)
    if result.error:
        print(f"[harness] {result.error}", file=sys.stderr)
    return 1 if result.status in {"partial", "failed"} else 0
