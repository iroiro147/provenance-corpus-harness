# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Deterministic acquisition fingerprints plus immutable per-attempt receipts with exact
  written, duplicate, empty, partial, failed, and resumed outcomes.
- Opt-in `--resume` and explicit `--refresh` execution controls.
- DNS-pinned HTTP transport with public-address validation on every redirect, response
  byte limits, cross-origin credential stripping, and protected robots retrieval.
- URL credential detection, secret-query sanitization, canonicalization, and error
  redaction utilities.

### Changed

- RSS and blog defaults now use the protected transport instead of library-managed URL
  fetching.
- Deduplication now requires the same source URL and content hash, preserving identical
  text collected from distinct sources.
- Collision paths are validated repeatedly and never blindly overwritten.
- Development version advanced to `0.2.0.dev0`; no v0.2 release has been published.

## [0.1.0] - 2026-07-14

### Added

- Offline-tested adapters for Hacker News, RSS/Atom, individual articles, YouTube,
  GitHub, Reddit, and Product Hunt.
- Atomic Markdown writes with YAML provenance, content hashes, and collision handling.
- Standalone package metadata and `corpus-harness` console command.
- CI, contribution, security, maintainer, source-policy, and dependency-license docs.
- Output-root containment for custom adapter platform identifiers and symlink targets.

### Changed

- Reframed the project around auditable provenance rather than a private downstream
  pipeline.
- Replaced absolute permission claims with an explicit operator-responsibility policy.

[Unreleased]: https://github.com/iroiro147/provenance-corpus-harness/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/iroiro147/provenance-corpus-harness/releases/tag/v0.1.0
