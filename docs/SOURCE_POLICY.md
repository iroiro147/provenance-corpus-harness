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

The blog adapter checks `robots.txt` by default and skips an explicitly disallowed URL.
The current helper treats a missing or unreachable robots file as inconclusive rather
than an explicit prohibition. That technical behavior is not permission: the operator
must still verify the source's terms and rights before collection.

## Proposed adapters

A new adapter is eligible for review only when its access surface and failure behavior
are documented, it has offline tests, and it avoids evasion. A maintainer may reject an
adapter even when it is technically feasible.
