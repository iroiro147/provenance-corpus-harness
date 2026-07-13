"""Command-line interface for provenance-first Markdown corpus collection."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from . import __version__
from .scrapers import SCRAPERS
from .scrapers.blog import BlogScraper
from .scrapers.github import GitHubScraper
from .scrapers.hackernews import HackerNewsScraper
from .scrapers.producthunt import ProductHuntScraper
from .scrapers.reddit import RedditScraper
from .scrapers.rss import RSSScraper
from .scrapers.youtube import YouTubeScraper


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
    args = p.parse_args(argv)

    scraped_at = datetime.now(timezone.utc).isoformat()
    scraper = build_scraper(args.platform, args.target, args.max_comments)
    print(
        f"[harness] {args.platform} ← {args.target} (limit {args.limit}) → {args.out}/",
        file=sys.stderr,
    )

    written = scraper.run(args.target, args.out, limit=args.limit, scraped_at=scraped_at)

    print(f"[harness] wrote {len(written)} corpus file(s)", file=sys.stderr)
    for pth in written:
        print(f"  {pth}")
    if not written:
        print("[harness] nothing written (empty / robots-disallowed / duplicate).", file=sys.stderr)
    return 0
