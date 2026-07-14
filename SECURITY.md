# Security Policy

## Supported versions

Before the first stable release, security fixes are applied to the latest commit on the
default branch. A supported-version table will be added when multiple release lines
exist.

## Reporting a vulnerability

Do not open a public issue for a vulnerability or suspected credential exposure. Use
GitHub's **Report a vulnerability** flow in the repository Security tab. Include:

- the affected version or commit;
- reproduction steps that do not target systems you do not own or control;
- the impact and any known mitigations; and
- whether the report involves a third-party source adapter.

The maintainer aims to acknowledge complete reports within seven days. No response-time
or bounty guarantee is made.

## Threat model

Collected text, metadata, source URLs, API responses, and generated Markdown are
untrusted input. Downstream users should not execute generated content, interpolate it
into shell commands, or render it as trusted HTML. Tokens are accepted only through
environment variables and must never be stored in corpus output or issue reports.

The project does not authorize testing against third-party services. Security research
must remain within systems you own or have explicit permission to test.

Adapter platform identifiers are validated as a single lowercase path segment, and the
writer verifies the resolved platform directory remains directly beneath the configured
output root. Report any path-containment bypass through the private channel above.

Content-addressed assets use hash-only filenames and are never executed or unpacked.
Source-package import rejects absolute and traversal paths, symlinks, special files,
hash or size mismatches, credential-like fields, session artifacts, and configured
count or byte-limit overruns. Evidence indexes accept declared records and assets only;
arbitrary directory contents are not swept implicitly.

The optional renderer is not an unrestricted browser. Its integration contract aborts
downloads, WebSockets, non-GET requests, popups, and service workers, and fulfills
allowed responses through the same every-hop validated transport used by static fetches.
It never auto-accepts consent or attempts access-control evasion.
