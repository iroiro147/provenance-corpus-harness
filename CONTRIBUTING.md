# Contributing

Thanks for improving Provenance Corpus Harness.

## Development setup

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

Run the complete local gate before opening a pull request:

```bash
ruff check .
ruff format --check .
pytest -q
python -m build
python -m twine check dist/*
python -m pip check
pip-audit
```

## Pull requests

- Keep each change focused and explain the source-policy impact.
- Add offline fixtures and tests; CI must not depend on live third-party services.
- Preserve `source_url`, `scraped_at`, and `content_hash` in every record.
- Never add credential logging, access-control bypasses, CAPTCHA solving, or stealth
  browser behavior.
- Do not commit generated corpora unless the content is redistributable and explicitly
  intended as a test fixture.
- Update the changelog when behavior or the output contract changes.

## Adding an adapter

An adapter must:

1. inherit from `BaseScraper` and yield `CorpusItem` objects;
2. document the official or explicitly authorized access surface;
3. use an injectable fetcher or runner so tests remain offline;
4. throttle live requests and handle denied access without bypassing it;
5. include an entry in `docs/SOURCE_POLICY.md`; and
6. avoid treating technical accessibility as permission to collect or redistribute.

Any adapter that opens remote URLs must use `SafeHttpTransport` or demonstrate an
equivalent every-hop DNS, redirect, credential, timeout, and byte-budget contract. Do
not delegate URL fetching to a parser or extraction library. New crawl, browser, media,
or managed-service surfaces require a separate threat model and offline adversarial
tests.

By contributing, you agree that your contribution is licensed under Apache-2.0.
