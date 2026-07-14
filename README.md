# Provenance Corpus Harness

[![CI](https://github.com/iroiro147/provenance-corpus-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/iroiro147/provenance-corpus-harness/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Collect source-linked Markdown records you can inspect, diff, and audit.**

Most collection scripts stop when they have the text. That is exactly where corpus
problems begin: URLs disappear, timestamps drift, content changes silently, and nobody
can explain which source produced which record.

Provenance Corpus Harness treats provenance as part of the record—not an afterthought in
a log file. Explicit source adapters produce portable Markdown with the source URL,
collection time, content hash, and platform metadata beside the collected text. Each run
also produces a deterministic acquisition receipt with exact outcomes and relative paths.

```text
FROM scraped text blobs
TO   source-linked corpus records
```

Not a crawler. Not a vector database. A provenance-first collection layer for people
building durable corpora from sources they are authorized to access.

## The problem: provenance debt

A folder of text can look like a corpus while quietly accumulating **provenance debt**:
the missing context that makes a dataset difficult to verify, refresh, or defend later.
If a record cannot answer where it came from, when it was collected, and whether its
body changed, downstream enrichment only compounds the uncertainty.

This harness makes that context a first-class contract:

```text
source adapter -> CollectionSpec -> CorpusItem -> record + acquisition receipt
```

Every adapter is explicit. Every output is ordinary Markdown. Every collection path is
offline-testable with injected fetchers or runners.

## What you get

- **Source-linked records** — source URLs, timestamps, hashes, and platform metadata live
  beside the content.
- **Portable output** — Markdown and YAML frontmatter work with Git, static tools, search
  indexes, and downstream corpus pipelines.
- **Change-aware writes** — only the same source plus the same body is a duplicate;
  collisions receive a stable hash suffix instead of overwriting history.
- **Bounded collection** — explicit adapters, clear authentication behavior, polite HTTP,
  robots checks where applicable, and no access-control evasion.
- **Offline-testable adapters** — the test suite does not require live network access.
- **Verifiable runs** — atomic JSON receipts distinguish written, duplicate, empty,
  partial, failed, and explicitly resumed acquisitions.
- **Safer remote fetches** — RSS and article fetches reject credentials and non-public
  addresses, revalidate every redirect hop, pin the resolved address, cap response bytes,
  and strip credentials on cross-origin redirects.

## Explicit source adapters

- Hacker News via the public Firebase API
- RSS and Atom feeds
- individual article pages with best-effort robots checks
- YouTube metadata and available transcripts via `yt-dlp`
- GitHub repository metadata, README files, and release notes via REST
- Reddit public JSON where available
- Product Hunt via its authenticated GraphQL API

## Who this is for

Use the harness when you are building a research corpus, retrieval system, knowledge
base, archive, or evaluation dataset and need collection evidence to survive beyond the
first script run.

It is deliberately the wrong tool for broad crawling, access-control bypass, content
laundering, embeddings, or model enrichment. Those are different jobs with different
trust boundaries.

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

# Reuse a matching complete receipt without calling the adapter again
corpus-harness rss https://example.com/feed.xml --out corpus --resume

# Explicitly execute again even when a receipt already exists
corpus-harness rss https://example.com/feed.xml --out corpus --refresh
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

## Acquisition receipts

Every CLI run writes an atomic, uniquely identified attempt receipt under
`<output>/_provenance/runs/<fingerprint>/`. The fingerprint covers the public
acquisition contract, adapter identity, sanitized target, an opaque target identity
hash, limit, adapter schema, and non-secret behavior options. `latest.json` indexes the newest attempt, while
`latest-complete.json` preserves the newest reusable success. Receipts contain only
relative record paths; absolute targets and error paths are redacted, and environment
variables are never serialized intentionally.

```json
{
  "contract": "provenance-acquisition.v1",
  "fingerprint": "<sha256>",
  "status": "complete",
  "counts": {"written": 2, "duplicates": 0, "empty": 1, "failed": 0},
  "paths": ["rss/example-one.md", "rss/example-two.md"]
}
```

`--resume` reuses only a matching `complete` receipt whose referenced files still
exist. Changed targets, limits, adapters, missing files, partial runs, and malformed
receipts execute again. `--refresh` always executes and is mutually exclusive with
`--resume`.

## The record contract

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

Writes are atomic. The same sanitized source URL and body hash is skipped; a distinct
source or changed body with the same slug receives a hash suffix. Platform identifiers
are restricted to one lowercase path segment, and resolved output paths must remain
below the configured root. Userinfo, fragments, and secret-bearing URL query fields are
removed before URLs enter records or receipts.
Treat all collected text and metadata as untrusted input in downstream systems.

The result is a corpus you can reason about later—not merely a directory you happened to
fill today.

## Architecture

```text
harness/
  acquisition.py     deterministic run receipts and opt-in resume
  base.py            CorpusItem, atomic writer, polite HTTP, robots helper
  cli.py             corpus-harness command
  transport.py       DNS-pinned, redirect-safe, byte-bounded HTTP(S)
  url_safety.py      URL validation, canonicalization, and redaction
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

### Capability boundary

This project shares acquisition design principles with the Atelier corpus pipeline, but
it is not a public mirror of the whole Atelier factory.

| In this package | Deliberately outside the core |
|---|---|
| Explicit source adapters | Factory pack orchestration and GRID synthesis |
| Provenance records and run receipts | Vector or multimodal retrieval |
| Safe static HTTP transport | Browser automation and overlay interaction |
| Deterministic resume and exact outcomes | Managed scraping services |
| Portable Markdown and JSON | Account-bound acquisition or access bypass |

Future crawl, browser, media, and asset adapters must remain optional and cannot land
until their transport, consent, rights, byte-budget, and offline-test contracts are
explicit.

## Contributing and governance

Start with [CONTRIBUTING.md](CONTRIBUTING.md). New adapters must document their access
surface, preserve provenance, avoid access-control bypasses, and include offline tests.
Maintainer responsibilities are recorded in [MAINTAINERS.md](MAINTAINERS.md), and
security reports follow [SECURITY.md](SECURITY.md).

## License

Apache-2.0. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
