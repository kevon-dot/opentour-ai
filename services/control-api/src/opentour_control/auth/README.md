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

Node uses the maintained jose signature/local-key implementation with a bounded
trusted-URL transport instead of its unbounded default JWKS body reader. Its
five-second deadline covers headers and the body stream; abort/cancel is requested
at the deadline or after 64 KiB. Redirects are refused, only 1–32 unique RSA/RS256
verification keys are admitted, and an unknown-key refresh does not block valid
cached keys. The Node SDK exchange/refresh transport has the same byte/time limits.

Python composition uses `create_workos_client`, which supplies the SDK's maintained
HTTPBackend interface. Its fixed WorkOS HTTPS origin, GET-only identity reader,
no-redirect policy, identity encoding and 1 MiB page body cap apply before SDK JSON
parsing. One worker serves one bounded five-second caller wait. Socket/DNS work may
finish later; no second worker is admitted meanwhile, and `close_complete` reports
actual shutdown. The same generated slow/oversize fixtures exercise this boundary.
Composition must close both the injected backend and client; no import starts I/O.

Migration `0001_identity_state.sql` is schema version 1 and runs under a separate
migration owner. Runtime connections require only schema USAGE and table
SELECT/INSERT/UPDATE, with no superuser, ownership, BYPASSRLS or DELETE privileges.
Session/generation/denial, continuation claims, provider observations and their
inbox/outbox/cursor are identity-owned state. OPE-171/172 own membership grants and
selection CAS; OPE-144 owns tenant pooling/RLS; OPE-146 consumes revocation intents
and owns authority journals/cell enforcement. The outbox is not enforcement proof.

The identity-owned `session_credentials` table stores only an opaque sealed payload,
issuer/session, generation and expiry. Required server keys are injected through
`FernetSealingPort`, using maintained cryptography MultiFernet authenticated
encryption. The first key writes and up to three older keys support explicit
rotation; there is no key/default loaded from browser input. The encrypted envelope
binds purpose, issuer, subject and session plus absolute expiry. Replacing keys
without a compatible decrypting key fails closed. Credentials and continuation
claims use distinct scopes. Ciphertext alone never bypasses the durable session
generation/ended/expiry checks. No key is stored in a database row or context.

Credential writes occur in the sole Python repository's admission/refresh
transaction. Final297 composition implements the declared TS ports by consuming
this server state and its trusted sealing configuration; it must not create an
independent SQL writer. The actual local probe uses finite test-only subprocess
pipes to exercise these factories without inventing a production transport route.
Generated provider HTTP remains simulated even when its session state uses real PG.

Before a provider organization switch, the adapter durably marks selected-context
admission as requiring reconciliation. Failure to store that marker prevents the
provider request. The172 local selection CAS alone clears the marker after an exact
verified target commit. A transient response does not end the identity session;
unknown provider or local acknowledgments do not restore the prior tenant context.

Maintained sealing reference: https://cryptography.io/en/latest/fernet/ . Fernet's
creation timestamp is visible in ciphertext; the credential payload is encrypted.

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
