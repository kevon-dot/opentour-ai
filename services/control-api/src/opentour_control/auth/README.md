# Human identity boundary

OPE-142 provides strict WorkOS token verification, durable identity state, a
session-context router factory, and server-only sign-in/callback factories. These
are configured components. OPE-297 owns final BFF/browser runtime composition;
OPE-202 owns control schema registration. Missing configuration refuses admission.

The provider must sign the configured audience via its AuthKit JWT template. The
issuer, audience, RS256, `sub`, `sid`, `auth_time`, issue time and expiry are checked.
`assurance_level: unknown` is unqualified metadata and never a strong-auth receipt.
Provider role names, domain matches and forwarded client context grant nothing.
The six-field verified identity serves only the finite own-account allowlist. The
full tenant context additionally requires current local membership and provider
freshness. Domain owners still check current capabilities/revision at effect commit.

The JWKS cache is an immutable snapshot valid for at most 300 seconds. One unknown
key can trigger a single coalesced refresh with a 30-second attempt cooldown.
Requests never queue behind the refresh lock; known keys in an unexpired snapshot
remain usable. The default caller wait is five seconds and late results are
discarded. The HTTP client also uses a five-second socket timeout and a 64 KiB
response bound. Python DNS resolution/socket progress may outlive the caller wait:
there is at most one owned retrieval thread until it actually exits. `close()`
reports false while it remains alive. This is not a claim of hard OS cancellation.

Migration `0001_identity_state.sql` is schema version 1 and runs under a separate
migration owner. Runtime connections require only schema USAGE and table
SELECT/INSERT/UPDATE, with no superuser, ownership, BYPASSRLS or DELETE privileges.
Session/generation/denial, continuation claims, provider observations and their
inbox/outbox/cursor are identity-owned state. OPE-171/172 own membership grants and
selection CAS; OPE-144 owns tenant pooling/RLS; OPE-146 consumes revocation intents
and owns authority journals/cell enforcement. The outbox is not enforcement proof.

Opaque browser bindings contain no credentials or tenant claims. Clearing a removed
selection advances the session generation and only the authenticated binding in
one transaction; other bindings remain stale. Reads recheck generation, selection,
ended/reconciliation state and current membership after provider I/O. Logout writes
a durable denial before a cookie can be cleared or a successful redirect emitted.
Unknown local commits do not produce success. No denial-retention job is installed.

The TypeScript adapter takes required durable transaction/session ports and the
OPE-172 selection port. Its test memory ports are explicitly simulated. Provider
exchange and SQL commits are separate operations; unknown results require reviewed
reconciliation, never automatic replay. SDK callback helpers are not used because
their cookie/log ordering precedes the required local commit boundary. Maintained
SDK token-exchange methods are configured with zero automatic mutation retries.

Generated JWT and mocked HTTP fixtures establish component behavior only. Actual
PostgreSQL tests must use the separately registered local-integration target and a
private disposable cluster. Live WorkOS configuration and the four-cell ISO-01
matrix remain separate provider gates; neither is satisfied by these fixtures.
