# Provenance Corpus Harness

Collect text from explicit source adapters and write portable Markdown records with
source URLs, timestamps, content hashes, and platform metadata.

The harness is intentionally a narrow gathering layer. It does not crawl the open web,
solve access challenges, execute downloaded content, or perform embeddings and model
enrichment. Its output is ordinary Markdown that can be inspected, versioned, and fed
to any downstream corpus pipeline.

## Why this exists

Corpus collection often loses the context needed to audit a dataset later. This project
keeps provenance next to every record and gives each source adapter an offline-testable
contract:

```text
source adapter -> CorpusItem -> <output>/<platform>/<slug>.md
```

Included adapters:

- Hacker News via the public Firebase API
- RSS and Atom feeds
- individual article pages with best-effort robots checks
- YouTube metadata and available transcripts via `yt-dlp`
- GitHub repository metadata, README files, and release notes via REST
- Reddit public JSON where available
- Product Hunt via its authenticated GraphQL API

## Responsible-use boundary

This software does not grant permission to collect or reuse content. An official API,
an RSS feed, or an allowing `robots.txt` entry is not a substitute for checking the
source's terms, content license, privacy obligations, and applicable law. The harness
does not bypass authentication, rate limits, CAPTCHAs, or access controls.

Read [the source policy](docs/SOURCE_POLICY.md) before running a live collection.

## Install

Python 3.11 or newer is required.

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
corpus-harness --version
```

The YouTube adapter also needs `yt-dlp` on `PATH`.

## Usage

```bash
# Hacker News: top|new|best|ask|show|job or a story id
corpus-harness hackernews top --out corpus --limit 25 --max-comments 10

# RSS / Atom
corpus-harness rss https://simonwillison.net/atom/everything/ --out corpus --limit 20

# One or more explicitly authorized article URLs
corpus-harness blog "https://example.com/post-a,https://example.com/post-b" --out corpus

# YouTube transcript + metadata; no video or frame download
corpus-harness youtube "VIDEO_ID,https://youtu.be/ANOTHER_ID" --out corpus

# GitHub REST API; GITHUB_TOKEN raises the API rate limit
corpus-harness github "psf/requests,tiangolo/fastapi" --out corpus

# Reddit public JSON; availability varies and the adapter fails without evasion
corpus-harness reddit r/programming/top --out corpus --limit 25

# Product Hunt requires an official developer token
PRODUCTHUNT_TOKEN=... corpus-harness producthunt featured --out corpus
```

### Access requirements

| Adapter | Authentication | Behavior when access is unavailable |
|---|---|---|
| Hacker News, RSS | None for normal use | Raises or returns no records |
| Blog | Site-specific | Skips robots-disallowed URLs; never bypasses a block |
| YouTube | Site-specific | Returns no record when metadata/transcript retrieval fails |
| GitHub | Optional `GITHUB_TOKEN` | Skips unavailable/rate-limited repositories |
| Reddit | Public JSON, where available | Emits a clear message and returns no records |
| Product Hunt | `PRODUCTHUNT_TOKEN` or `PH_TOKEN` | Raises a clear configuration error |

Tokens are read from the environment and are never written into corpus records.

## Output contract

Each record is written to `<output>/<platform>/<slug>.md`:

```markdown
---
platform: hackernews
source_url: https://news.ycombinator.com/item?id=1
title: Example
author: alice
date: '2026-07-13'
scraped_at: '2026-07-13T12:00:00+00:00'
content_hash: <sha256-of-body>
extra:
  score: 42
---

# Example

Collected prose.
```

Writes are atomic. An identical body at the same slug is skipped; a changed body with
the same slug receives a short hash suffix. Platform identifiers are restricted to one
lowercase path segment, and resolved output paths must remain below the configured root.
Treat all collected text and metadata as untrusted input in downstream systems.

## Architecture

```text
harness/
  base.py            CorpusItem, atomic writer, polite HTTP, robots helper
  cli.py             corpus-harness command
  scrapers/          one explicit adapter per source surface
tests/                offline tests with injected fetchers and fixtures
```

Every adapter accepts an injectable fetcher or runner, so the test suite uses no live
network:

```bash
python -m pip install -e '.[dev]'
ruff check .
ruff format --check .
pytest -q
python -m build
python -m twine check dist/*
```

## Contributing and governance

Start with [CONTRIBUTING.md](CONTRIBUTING.md). New adapters must document their access
surface, preserve provenance, avoid access-control bypasses, and include offline tests.
Maintainer responsibilities are recorded in [MAINTAINERS.md](MAINTAINERS.md), and
security reports follow [SECURITY.md](SECURITY.md).

## License

Apache-2.0. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
