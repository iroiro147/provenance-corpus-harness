# Source policy

Provenance Corpus Harness is a collection tool, not a grant of rights. Operators are
responsible for confirming that collection, storage, and reuse are permitted for their
specific source, jurisdiction, and purpose.

## Non-negotiable boundaries

- Do not bypass authentication, access controls, rate limits, CAPTCHAs, paywalls, or
  technical blocks.
- Do not collect personal or sensitive data without a lawful basis and a documented
  retention policy.
- Do not assume that public accessibility, an allowing `robots.txt`, or API availability
  grants copyright or redistribution rights.
- Use source-provided APIs and credentials where required, respect documented quotas,
  and stop when a source denies access.
- Keep provenance metadata attached to collected records.
- Never put credentials in a source URL. The harness rejects credentials before live
  fetching and removes userinfo, fragments, and secret-bearing query fields before a URL
  enters a record, receipt, or error message.

## Adapter review

| Adapter | Access surface | Important operator checks |
|---|---|---|
| Hacker News | Public Firebase API | API policy, content reuse, privacy, request volume |
| RSS / Atom | Publisher-provided feed | Feed terms, article license, redistribution scope |
| Blog | Explicit URLs, best-effort robots check | Site terms, copyright, privacy, robots availability |
| YouTube | Locally installed `yt-dlp` | Platform terms, transcript availability, content rights |
| GitHub | REST API | API terms, repository license, rate limit, private data |
| Reddit | Public JSON where available | Platform terms, deleted content, personal data, access denial |
| Product Hunt | Authenticated GraphQL API | Developer agreement, token scope, API quota |

## Robots behavior

The blog adapter checks `robots.txt` by default through the same public-address-validated,
redirect-safe transport used for article content and skips an explicitly disallowed URL.
The current default treats a missing or unreachable robots file as inconclusive rather
than an explicit prohibition. That technical behavior is not permission: the operator
must still verify the source's terms and rights before collection.

## Receipts are evidence, not permission

An acquisition receipt proves what the tool attempted and wrote under a particular
public configuration. It does not prove ownership, license, consent, or lawful reuse.
Operators should keep their rights and retention decisions beside the corpus and review
them independently of the technical receipt.

## Rights-aware assets and media

Binary assets are stored as inert, content-addressed bytes. A rights declaration records
the operator's stated basis and permitted uses; it is evidence of that declaration, not
legal proof. Direct media acquisition is disabled by default and requires both an
explicit download choice and a valid source policy. Archives are never expanded, media
types and byte counts are bounded, and collected bytes are never executed.

## Account-bound sources are export-only

The public package does not log in to accounts, replay sessions, read cookie stores, or
import browser profiles. An account-bound source must first become a local export
obtained through an official API, a source-provided export, or another operator-approved
route. The package validates the export manifest, paths, hashes, sizes, source policy,
and recognized credential/session patterns before importing declared files. Pattern
checks are defense in depth, not proof that an export contains no sensitive data.

Paid, private, and unknown-rights material must remain local-only. Export packages do
not make restricted content public or redistributable.

## Browser and crawl behavior

Site collection is bounded, same-origin by default, robots-aware, and constrained by
explicit depth, page, byte, type, redirect, and delay limits. Optional rendering does
not auto-accept consent, solve challenges, dismiss paywalls, or navigate authentication.
Network responses exposed to the renderer must pass through the protected HTTP transport.

## Local evidence index

Only declared corpus records and assets enter the index. This release has no remote
provider or model-egress seam: indexing and querying are local. Citations preserve
source, locator, access, authorization, rights, local-only constraints, and hash
evidence. Similarity is discovery evidence, not a license or factual endorsement.

## Proposed adapters

A new adapter is eligible for review only when its access surface and failure behavior
are documented, it has offline tests, and it avoids evasion. A maintainer may reject an
adapter even when it is technically feasible.
