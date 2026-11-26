# Private pilot delivery lease

Use the same custody-controlled qualification register for the manual operator and automated
adapter. A restored worker reopens this authoritative register. A worker checkpoint or a copied
register is historical evidence and must never authorize new live actions. If the latest register
generation is uncertain, hold live work until the operator reconciles the original record.
Local starter planning can continue using generated inputs.

## Operation entry

Store the policy and signed command in owner-controlled mode-0600 files inside mode-0700
directories. The authority schema `opentour.pilot.lease-authority.v1` binds
`organization_ref`, `signer_ref`, `route` (`manual` or `automated`), `expires_at` as Unix seconds,
permitted `authorized_actions`, and a distinct 32-byte key as `verification_key_hex`.
Provision owner/custodian acceptance and operational/reconciliation authority separately.
Choose the authority file from trusted local configuration.

From the extracted kit directory with Python 3.12.16, run

```sh
python opentour-slack.pyz cohort-lease \
  --register /private/cohort/qualification.sqlite3 \
  --authority /private/cohort/lease-authority.json \
  --submission /private/cohort/lease-command.json
```

In the source environment, use `python -m opentour_cell.pilot.cohort_lease` with the same arguments.

Send a `command` and lowercase hexadecimal HMAC-SHA-256 `signature`. Sign the command version,
a newline, and its canonical JSON using the [qualification encoding](pilot-qualification.md).
Every command contains `schema_version=opentour.pilot.lease-command.v1`, `event_id`, `action`,
`organization_ref`, `engagement_revision`, `generation`, `slot` (1–10), `signer_ref`, `route` and
opaque SHA-256 `evidence_digest`. A display-name change cannot change the stable legal entity key.

| Action | Additional fields | Scope |
| --- | --- | --- |
| `acquire` | `qualification_digest`, `expires_at` | Bind to the current OPE-451 event digest. The signer verifies current owner/custodian acceptance for this engagement and binds its evidence digest. Use generation 0 before the first acquisition. |
| `begin` | `lease_ref` | Consume current lease authority for one immutable work scope. The resource adapter must use the same work-scope digest on retries and call this immediately before connecting. A pending effect is recorded before any connection. |
| `reconcile` | `lease_ref`, `effect_event_id`, `outcome` | Bind independent disposition evidence to the exact unresolved effect. Unknown outcomes stay pending. |
| `close` | `lease_ref`, `terminal_kind`, `reconciliation_revision`, `reconciliation_digest` | Record signed closeout or withdrawal. The terminal state and receipt commit before capacity can transfer. |

Both acquisition routes share one SQLite write transaction and one global open-lease constraint.
An exact replay of a current winning acquisition returns its original receipt. A replay of a
queued request never promotes it. After capacity opens, the earliest qualified, unexpired
waiter may present a fresh signed acquisition. Opening the register alone acquires nothing.

An expired lease cannot permit access and still occupies capacity until signed closeout.
Loss or replacement of a qualification revision blocks new access through the old lease.
A fresh charter and engagement revision are required after the terminal engagement.
If a `begin` acknowledgment is lost, reconcile its exact effect. Replaying the command or
reusing its work scope with a new command ID cannot authorize a second start.

The CLI and callable register perform only local ledger operations. Generated fixtures use
both signed routes. The per-slot production adapters must bind this register and use the
`begin` gate immediately before authorized live connection. Actual installed connection
evidence and OPE-357's distinct accepted-customer count remain separate.

From the pinned environment run
`just test-python services/cell-runtime/tests/pilot/test_cohort_lease.py`. The tests inspect
durable rows for concurrency, stable replay, holder restart, expiry, pending reconciliation,
signed closeout, scoped denial, and one legal identity across engagement revisions.

## Independent terminal review

Use three distinct signer identities and keys per organization. The operator may hold
`acquire`, `begin`, or both. The reconciler only holds `reconcile`. The terminal signer
only holds `close`. An action cannot reuse another role's signer or key for the same
organization. The journal records the signing role, signer reference, and a key fingerprint
for audit. It never stores the signing key.

For a proven effect, submit `outcome=observed_applied` or `observed_no_effect` and sign the
independent evidence digest. The journal retains the outcome, evidence digest, and
reconciler. Unknown outcomes stay pending until confirmed.

An acquisition receipt includes `reconciliation_revision` and `reconciliation_digest`.
Every effect start or recorded reconciliation changes the checkpoint. After the last pending
effect clears, pass the latest successful reconciliation receipt to the terminal signer.
A terminal command signs both checkpoint fields and the terminal evidence digest. An older
signature cannot authorize closeout after the pending work clears. If no effect started,
the original acquisition checkpoint is valid for terminal review.

## Cohort workplace connection profile

For each cohort installation configure the private workplace connection with
`schema_version=opentour.pilot.workplace-connection.cohort.v1` and add `cohort_lease`.
It names the already-existing canonical absolute `register` and three private filenames
beside the worker connection: `authority_file`, `discover_submission_file`, and
`acquire_submission_file`. All slots select the same authoritative register. Missing files
or an unavailable register hold the connection.

For each selected discovery or acquisition, the trusted operator signs one `begin` command
whose `evidence_digest` is the canonical digest of the array
`["opentour.pilot.workplace-lease-effect.v1", organization, engagement, connection_revision, operation]`.
The workplace connection document binds these paths and is included in its ordinary
connection digest. The provider process starts only after the signed command is consumed.
The runtime rechecks it before the catalog, each row read, and snapshot publication. The
reconciler records its observed outcome. A fresh source operation requires a new authorized
scope or connection revision. Standalone legacy connection documents keep their deployed
schema. The cohort installer selects this profile for any workplace source used by a slot.

This profile protects the existing workplace connector. The starter-to-first-observed feed
requires the persisted binding, source checkpoint, and reader owned by OPE-514. Its adapter
must check the same register at the provider boundary. Existing local dataset onboarding
registers an operator-selected file and does not dispatch that feed.
