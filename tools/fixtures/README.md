# Deterministic provider and dual-mode harnesses

This is the OPE-124 / OT-BOOT-C03 fixture implementation. Its generated identities,
scripted provider observations and local database records are test evidence.
Every observation sets `product_acceptance: NOT_RUN`; none qualifies a cloud,
billing, customer, signing or GPU gate.

Run the public commands after the locked CPU setup:

```sh
just test-python tests/fixtures/test_registry.py
just test-python tests/contract/test_harness_modes.py
just test-python tests/contract/test_local_harness_transport.py
just test-integration local harness-smoke
```

The local profile requires PostgreSQL **18.4** `postgres`, `initdb` and `psql` on
PATH (or the explicit installed Homebrew path). It records exact resolved binary
hashes and verifies versions. Missing tools produce BLOCKED/exit 3; it never
downloads packages, starts a package service or accepts a database URL. It uses a
new marked private directory and Unix socket with no TCP listener. At most eight
connections and 16 MiB shared buffers are configured. SQL, helper output,
startup, statements and total runner time are bounded.

Assertions run through real pytest inventory. A missing/zero/skipped test,
assertion failure or failed teardown cannot pass. Actual SQL runs as the verified
non-owner, non-superuser, non-BYPASSRLS runtime role. Tests prove RLS with omitted
application predicates, transaction-local context reset after commit/rollback/
error/timeout, tenant-qualified foreign keys, and one conditional mutation plus
outbox under concurrent requests in both modes. These are fixture mechanics for
OPE-144, not qualification of an absent production connection pool.

Normal shutdown waits for the owned PostgreSQL postmaster's clean smart-shutdown
exit. An abnormal/forced exit or aborted helper retains the marked private
directory and records failed cleanup for recovery. The adverse lifecycle test
stops only observed children of its own newly created postmaster, proves that a
killed postmaster does not justify directory deletion, then resumes those
children and verifies their exit before removing its marked directory.

## Fixture contract and observations

`registry.json` binds version, seed, UTC clock, distinct mode/tenant/cell IDs,
script and expected effects/ledger entries. The scenario execution produces
observations independently of those expectations. Replaying fresh local state
with the same seed produces the same logical digest. The fixture digest uses a
stable test serialization; it is not production RFC 8785 signing.

The following named scripts execute in both `customer_controlled` and
`opentour_managed` modes:

| Concern | Registered cases |
| --- | --- |
| Durable local transactions | before_local_commit, after_local_commit, normal |
| Ambiguous provider acceptance | provider_accept_before_ack, duplicate_provider_acceptance, lost_heartbeat |
| Events and source reconciliation | duplicate_event, out_of_order_event, delayed_source_outcome, unknown_membership |
| Authority and isolation | expired_guard, unavailable_journal_head, missing_ledger_sequence, cross_tenant, revoked_epoch, late_receipt |
| Recovery | cleanup_failure |

Lost acknowledgment remains unknown until exact paginated reconciliation finds
one matching job; an empty lookup never authorizes resubmission. Multiple exact
jobs quarantine. A cancellation acknowledgment is separate from a terminal
observation. Historical receipts may reconcile old effects but cannot authorize
new publication after revocation. Missing journal frontiers/sequences refuse
dependent effects. Guard expiry compares UTC instants, including equivalent
timestamp precision at the exact boundary.

The actual local SQLite store persists guard/effect/outbox transactions and
restart state. It does not emulate DynamoDB IAM, remote consistency, SDK retries,
transaction limits or provider behavior. The local profile contains both local
and simulated observations; provider fakes retain an explicit simulated issuer
and namespace even when their consumer uses actual local persistence.

The command-owned report has runner `pytest`, kind `local_integration`, a unique
binding nonce and every actual test outcome. Its sidecar
`REPORT.observations.json` is validated against the closed
`observation.schema.json`, includes no private recovery paths or arbitrary
subprocess output, and keeps `product_acceptance: NOT_RUN` on every record.
Default Python process/network guards remain inherited; only the registered
native database process uses its private Unix socket. These guards are test
discipline, not hostile native-code containment.

## Consumer interfaces and acceptance mapping

`ports.py` defines harness interfaces for the separately owned production
consumers: OPE-144 tenant/database mechanics, OPE-146 authority/effects,
OPE-149 ambiguous provider reconciliation, and OPE-126 empirical aggregation.
No production adapter is claimed to exist or be reused. Canonical authority
guards are validated through the accepted local schema registry; no duplicate
production schema or signing implementation is introduced.

| Acceptance | Executed assertion |
| --- | --- |
| OT-BOOT-C03-AC01 | Accepted submit with lost acknowledgment, consumer restart, exact lookup, one provider submit |
| OT-BOOT-C03-AC02 | Same logical outcomes in both modes with distinct tenant/cell/submission identities |
| OT-BOOT-C03-AC03 | All twelve directed four-cell scope refusals, revoked guard, late historical receipt without new authority |
| ISO-05 fixture mapping | Scoped queue delivery, same-ID replay, foreign-scope refusal |
| OPS-02 fixture mapping | Unknown provider outcome, expired/revoked guard, acknowledged cancellation still running |
| OPS-03 fixture mapping | Shared interface/version and both-mode semantic parity |
| G4 fixture mapping | Named injected faults, durable recovery and explicit cleanup failure |

ISO/OPS/G4 full product gates remain separately owned and cannot consume these
fixtures as empirical PASS. `route_empirical` rejects simulated/local evidence
before a separately owned real collector is called. It does not authenticate
real receipts or substitute for that collector.

Governing source: [OPE-124](https://linear.app/opentourai/issue/OPE-124/create-deterministic-provider-and-dual-mode-failure-harnesses),
publication specification SHA-256
`efa0776162648087da41dfac3db658016b74ff200ba256e63960a1f0b6c3b8db`.
Provider reconciliation follows [OPE-149](https://linear.app/opentourai/issue/OPE-149/reconcile-ambiguous-provider-submissions-before-retrying)
and authority semantics follow the [authority protocol](https://linear.app/opentourai/document/authority-local-effects-and-revocation-completion-e77116ebeed7).
