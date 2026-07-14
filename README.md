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
through an adapter, the site collector, or the browser renderer also produces a
deterministic acquisition receipt with exact outcomes and relative paths.

```text
FROM scraped text blobs
TO   source-linked corpus records
```

A bounded collector and local evidence index—not a stealth crawler or hosted vector
service. It is a provenance-first substrate for people building durable corpora from
sources they are authorized to access.

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
- **Bounded site collection** — a same-origin frontier applies explicit page, depth,
  byte, content-type, robots, and politeness budgets.
- **Controlled browser rendering** — an optional browser adapter receives network bodies
  only through the protected HTTP transport and never accepts consent or evades access.
- **Rights-aware assets and media** — inert, content-addressed originals retain access,
  authorization, rights, byte, hash, and source declarations.
- **Export-only account sources** — operator-approved export packages reject recognized
  credential/session patterns and import without browser profiles or session replay.
- **Local evidence retrieval** — deterministic text retrieval and optional local visual
  similarity return portable citations with exact source locators and hashes.

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

It is deliberately the wrong tool for unbounded crawling, access-control bypass,
content laundering, session automation, or covert remote processing. Those are
different jobs with different trust boundaries.

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

Optional browser rendering and local image similarity are installed explicitly:

```bash
python -m pip install -e '.[browser]'
playwright install chromium
python -m pip install -e '.[multimodal]'
```

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

# Bounded same-origin site collection; rendering is explicit and optional
corpus-harness site https://example.com/docs --out corpus --max-pages 25 --max-depth 2
corpus-harness site https://example.com/app --out corpus --render
corpus-harness site-verify corpus/_provenance/site-runs/<receipt>.json --out corpus
corpus-harness browser https://example.com/app --out corpus
corpus-harness browser-verify corpus/_provenance/browser-runs/<receipt>.json --out corpus

# Direct media stays metadata-only unless both consent and a rights policy are explicit
corpus-harness media https://example.com/image.png --out corpus \
  --policy ./rights-policy.json
corpus-harness media https://example.com/image.png --out corpus \
  --policy ./rights-policy.json --download-media

# Classify a gated source before collection (exit 3 means an export is required)
corpus-harness source check https://x.com/example

# Discover, validate, import, and verify a local export package
corpus-harness package discover ./my-export \
  --manifest ./my-export/source-package.json \
  --package-id my-export-2026 --policy ./rights-policy.json
corpus-harness package validate ./my-export/source-package.json
corpus-harness package import ./my-export/source-package.json --out imported/my-export
corpus-harness package verify imported/my-export

# Build, query, and verify a deterministic local evidence index
corpus-harness index build --corpus corpus --out evidence-index
corpus-harness index query evidence-index --text "source lineage"
corpus-harness index query evidence-index --image query.png
corpus-harness index verify evidence-index --corpus corpus
```

A rights policy is an operator declaration, not legal proof. The minimal public-source
form is:

```json
{
  "rights": {"status": "public", "permitted_uses": ["local-research"]},
  "authorization": {"basis": "public"},
  "access_class": "public",
  "local_only": false
}
```

Use `account-owned-export`, `operator-approved-export`, `official-api`, or
`licensed-dataset` only with the corresponding evidence. Paid, private, and
unknown-rights sources must remain `local_only: true`.

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

Every explicit source-adapter acquisition run writes an atomic, uniquely identified
attempt receipt under
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

Bounded site collections and browser renders write separate immutable receipts. Media
and browser assets write a verifiable asset manifest, package imports write an import
receipt, and evidence indexes write a content-addressed index manifest. Read-only source
checks and queries do not create acquisition receipts.

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
  assets.py           inert content-addressed asset storage and manifests
  base.py            CorpusItem, atomic writer, polite HTTP, robots helper
  browser.py          optional renderer with transport-fulfilled network access
  cli.py             corpus-harness command
  crawl.py           bounded same-origin frontier and site collection
  evidence/          deterministic local text/image retrieval and verification
  media.py           explicit-consent direct media acquisition
  rights.py          rights, authorization, access, and local-only declarations
  source_gates.py    public-vs-export-required source classification
  source_package.py  pattern-checked export discovery, import, and verification
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

The public package includes explicit source adapters, bounded crawling, controlled
rendering, rights-aware assets and media, verifiable export-package import, and a local
evidence index. Heavy browser and image dependencies remain optional.

It does not include credential capture, cookie replay, browser-profile import, login or
paywall automation, CAPTCHA solving, stealth behavior, consent auto-acceptance, or
remote model egress. Account-bound sources cross the public boundary only as declared
exports whose paths, hashes, sizes, policies, and recognized credential/session patterns
can be checked. Pattern checks reduce accidental leakage; they are not a proof that an
export contains no sensitive data.

## Contributing and governance

Start with [CONTRIBUTING.md](CONTRIBUTING.md). New adapters must document their access
surface, preserve provenance, avoid access-control bypasses, and include offline tests.
Maintainer responsibilities are recorded in [MAINTAINERS.md](MAINTAINERS.md), and
security reports follow [SECURITY.md](SECURITY.md).

## License

Apache-2.0. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
