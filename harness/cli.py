"""Command-line interface for provenance-first Markdown corpus collection."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

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


_EXTENDED_COMMANDS = {
    "browser",
    "browser-verify",
    "index",
    "media",
    "package",
    "site",
    "site-verify",
    "source",
}


def _read_source_policy(path: str):
    from .rights import SourcePolicy

    policy_path = Path(path).expanduser().resolve()
    body = policy_path.read_bytes()
    if len(body) > 1024 * 1024:
        raise ValueError("source policy exceeds 1048576 bytes")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("source policy must be a JSON object")
    return SourcePolicy.from_mapping(value)


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".json-", delete=False
    ) as handle:
        json.dump(value, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _extended_main(argv: list[str]) -> int:  # noqa: C901 - command routing is intentionally flat
    command = argv[0]
    if command == "source":
        parser = argparse.ArgumentParser(prog="corpus-harness source")
        sub = parser.add_subparsers(dest="action", required=True)
        check = sub.add_parser("check", help="classify a source before acquisition")
        check.add_argument("url")
        check.add_argument("--marker", action="append", default=[])
        args = parser.parse_args(argv[1:])
        from .source_gates import classify_source_gate

        decision = classify_source_gate(args.url, markers=args.marker)
        print(json.dumps(asdict(decision), indent=2, sort_keys=True))
        return 3 if decision.action == "export_required" else 0

    if command == "package":
        parser = argparse.ArgumentParser(prog="corpus-harness package")
        sub = parser.add_subparsers(dest="action", required=True)
        discover = sub.add_parser("discover", help="create a manifest for a local export")
        discover.add_argument("directory")
        discover.add_argument("--manifest", required=True)
        discover.add_argument("--package-id", required=True)
        discover.add_argument("--connector", default="generic-export")
        discover.add_argument("--policy", required=True)
        discover.add_argument("--metadata")
        discover.add_argument("--max-item-bytes", type=int, default=100 * 1024 * 1024)
        discover.add_argument("--max-total-bytes", type=int, default=2 * 1024 * 1024 * 1024)
        discover.add_argument("--max-items", type=int, default=1000)
        validate = sub.add_parser("validate", help="validate a source-package manifest")
        validate.add_argument("manifest")
        importer = sub.add_parser("import", help="import a validated source package")
        importer.add_argument("manifest")
        importer.add_argument("--out", required=True)
        importer.add_argument("--max-item-bytes", type=int, default=100 * 1024 * 1024)
        importer.add_argument("--max-total-bytes", type=int, default=2 * 1024 * 1024 * 1024)
        importer.add_argument("--max-items", type=int, default=1000)
        verify = sub.add_parser("verify", help="verify an imported source package")
        verify.add_argument("directory")
        args = parser.parse_args(argv[1:])
        from .source_package import (
            discover_source_package,
            import_source_package,
            load_source_package,
            verify_import,
        )

        if args.action == "discover":
            manifest = discover_source_package(
                args.directory,
                package_id=args.package_id,
                connector=args.connector,
                source_policy=_read_source_policy(args.policy),
                metadata_path=args.metadata,
                max_item_bytes=args.max_item_bytes,
                max_total_bytes=args.max_total_bytes,
                max_items=args.max_items,
            )
            destination = Path(args.manifest).expanduser().resolve()
            package_root = Path(args.directory).expanduser().resolve()
            if destination.parent != package_root or destination.name != "source-package.json":
                raise ValueError("manifest must be <export-directory>/source-package.json")
            _atomic_json(destination, manifest.to_dict())
            print(destination)
            return 0
        if args.action == "validate":
            manifest = load_source_package(args.manifest)
            print(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
            return 0
        if args.action == "import":
            result = import_source_package(
                args.manifest,
                args.out,
                max_item_bytes=args.max_item_bytes,
                max_total_bytes=args.max_total_bytes,
                max_items=args.max_items,
            )
            print(json.dumps(asdict(result), indent=2, sort_keys=True, default=str))
            return 0
        result = verify_import(args.directory)
        print(json.dumps(asdict(result), indent=2, sort_keys=True))
        return 0 if result.ok else 1

    if command == "media":
        parser = argparse.ArgumentParser(prog="corpus-harness media")
        parser.add_argument("url")
        parser.add_argument("--out", required=True)
        parser.add_argument("--policy", required=True)
        parser.add_argument("--download-media", action="store_true")
        parser.add_argument("--max-bytes", type=int, default=25 * 1024 * 1024)
        args = parser.parse_args(argv[1:])
        from .assets import AssetStore, merge_asset_manifest
        from .media import MediaPolicy, acquire_direct_media

        store = AssetStore(args.out, max_asset_bytes=args.max_bytes)
        result = acquire_direct_media(
            args.url,
            policy=MediaPolicy(download_media=args.download_media, max_asset_bytes=args.max_bytes),
            source_policy=_read_source_policy(args.policy),
            asset_store=store,
        )
        if result.asset is not None:
            merge_asset_manifest(Path(args.out) / "assets.json", [result.asset])
        print(json.dumps(asdict(result), indent=2, sort_keys=True, default=str))
        return 0

    if command == "browser-verify":
        parser = argparse.ArgumentParser(prog="corpus-harness browser-verify")
        parser.add_argument("receipt")
        parser.add_argument("--out", required=True)
        args = parser.parse_args(argv[1:])
        from .browser import verify_browser_receipt

        result = verify_browser_receipt(args.receipt, args.out)
        print(json.dumps(asdict(result), indent=2, sort_keys=True))
        return 0 if result.ok else 1

    if command == "site-verify":
        parser = argparse.ArgumentParser(prog="corpus-harness site-verify")
        parser.add_argument("receipt")
        parser.add_argument("--out", required=True)
        args = parser.parse_args(argv[1:])
        from .crawl import verify_site_receipt

        result = verify_site_receipt(args.receipt, args.out)
        print(json.dumps(asdict(result), indent=2, sort_keys=True, default=str))
        return 0 if result.ok else 1

    if command == "site":
        parser = argparse.ArgumentParser(prog="corpus-harness site")
        parser.add_argument("url")
        parser.add_argument("--out", required=True)
        parser.add_argument("--max-pages", type=int, default=25)
        parser.add_argument("--max-depth", type=int, default=1)
        parser.add_argument("--max-page-bytes", type=int, default=5 * 1024 * 1024)
        parser.add_argument("--max-total-bytes", type=int, default=25 * 1024 * 1024)
        parser.add_argument("--min-interval", type=float, default=0.5)
        parser.add_argument("--render", action="store_true")
        args = parser.parse_args(argv[1:])
        from .browser import PlaywrightBrowserDriver
        from .crawl import CrawlPolicy, collect_site, write_site_receipt

        result = collect_site(
            args.url,
            args.out,
            policy=CrawlPolicy(
                max_pages=args.max_pages,
                max_depth=args.max_depth,
                max_page_bytes=args.max_page_bytes,
                max_total_bytes=args.max_total_bytes,
                min_interval=args.min_interval,
            ),
            renderer=PlaywrightBrowserDriver() if args.render else None,
        )
        receipt = write_site_receipt(result, args.out)
        payload = asdict(result)
        payload["receipt_path"] = str(receipt)
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 1 if result.status in {"partial", "failed"} else 0

    if command == "browser":
        parser = argparse.ArgumentParser(prog="corpus-harness browser")
        parser.add_argument("url")
        parser.add_argument("--out", required=True)
        parser.add_argument("--ready-selector")
        parser.add_argument("--screenshot", action="store_true")
        parser.add_argument("--policy", help="rights policy JSON required for screenshots")
        args = parser.parse_args(argv[1:])
        from .base import CorpusItem, html_to_text, write_corpus_item_result
        from .browser import (
            BrowserPolicy,
            PlaywrightBrowserDriver,
            render_page,
            write_browser_receipt,
        )

        if args.screenshot and not args.policy:
            raise ValueError("--screenshot requires --policy")

        page = render_page(
            args.url,
            driver=PlaywrightBrowserDriver(),
            policy=BrowserPolicy(
                ready_selector=args.ready_selector, capture_screenshot=args.screenshot
            ),
        )
        result = write_corpus_item_result(
            CorpusItem(
                platform="browser",
                source_url=page.final_url,
                title=page.title,
                body=html_to_text(page.html),
                extra={
                    "status_code": page.status_code,
                    "content_type": page.content_type,
                    "request_count": page.request_count,
                    "network_bytes": page.network_bytes,
                },
            ),
            args.out,
        )
        asset_ids: list[str] = []
        if page.screenshot is not None:
            from .assets import AssetStore, merge_asset_manifest

            store = AssetStore(args.out)
            asset = store.put(
                page.screenshot,
                source_url=page.final_url,
                media_type="image/png",
                source_policy=_read_source_policy(args.policy),
                role="browser-screenshot",
                alt=page.title,
            )
            merge_asset_manifest(Path(args.out) / "assets.json", [asset])
            asset_ids.append(asset.asset_id)
        receipt = write_browser_receipt(
            page,
            result.path,
            args.out,
            outcome=result.outcome,
            asset_ids=tuple(asset_ids),
        )
        payload = asdict(result)
        payload["receipt_path"] = str(receipt)
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0 if result.outcome != "empty" else 1

    parser = argparse.ArgumentParser(prog="corpus-harness index")
    sub = parser.add_subparsers(dest="action", required=True)
    build = sub.add_parser("build", help="build a deterministic local evidence index")
    build.add_argument("--corpus", required=True)
    build.add_argument("--out", required=True)
    query = sub.add_parser("query", help="query a local evidence index")
    query.add_argument("index")
    query.add_argument("--text")
    query.add_argument("--image")
    query.add_argument("--limit", type=int, default=10)
    verify = sub.add_parser("verify", help="verify a local evidence index")
    verify.add_argument("index")
    verify.add_argument("--corpus", required=True)
    args = parser.parse_args(argv[1:])
    from .evidence import build_evidence_index, query_evidence_index, verify_evidence_index

    if args.action == "build":
        result = build_evidence_index(args.corpus, args.out)
    elif args.action == "query":
        result = query_evidence_index(
            args.index, text=args.text, image_path=args.image, limit=args.limit
        )
    else:
        result = verify_evidence_index(args.index, corpus_dir=args.corpus)
    print(json.dumps(asdict(result), indent=2, sort_keys=True, default=str))
    return 0 if not hasattr(result, "ok") or result.ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in _EXTENDED_COMMANDS:
        try:
            return _extended_main(argv)
        except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            print(f"[harness] rejected: {redact_sensitive_text(str(exc))}", file=sys.stderr)
            return 2
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
