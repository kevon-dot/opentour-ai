# Reconcile delivery from independent recipient custody

The local receiver binds one enrolled signed package, its recipient and owner
trust anchor, and the current release's tenant, engagement, proposal revision
and artifact digest. The authenticated sender supplies the committed release
reference. The destination id comes from the recipient entitlement and the
pinned owner key. A different tenant or package cannot access that slot.

The dispatcher claims the durable outbox entry before offering the package.
The receiver checks the current sender claim before reading the offer,
verifies the signed handoff, and checks both the recipient grant and sender
claim again when it publishes the staged bytes and receipt. The
recipient stores one complete directory per signed specification and
destination. Its receipt is signed by the recipient key and checked
against the certificate pinned in the encrypted package. Each lookup
checks the stored bytes, signed proposal and artifact identity, and
recipient signature before returning the opaque receipt, destination and
observed time. Unknown or corrupt state is held. The sender's status reads
only inspect the outbox.

The generated qualification uses a real managed comparison, private Slack
approval, encrypted package, signed recipient entitlement and separate
recipient directory. It checks a lost response after the receiver installs,
repeated delivery, sender restart, readback, independent replay and fallback,
wrong scope and artifact, and withdrawal before the final write.

## Customer-local destination permission

Bind the worker outbox to the existing training store with
`handoff_outbox(store)`. Its private store directory contains an owner-only
`handoff-permission-key.json` with the custodian's 32-byte verification key
encoded as lowercase hex in `key`. A workflow uses
`handoff-permission-<digest>.json`, where the digest hashes the canonical
array `[tenant, workflow_ref]`. The file has `grant` and `seal_sha256` keys.
The seal is HMAC-SHA256 over the canonical UTF-8 grant.

The grant uses `opentour.pilot.handoff-permission.v1` as `schema_version`
and `deliver_approved_artifact` as `purpose`. It binds `tenant`,
`engagement`, `workflow_ref`, `proposal_ref`, `proposal_revision`,
`artifact_sha256`, `recipient`, `destination_ref`, `grant_ref`, the Unix
seconds deadline `expires_at`, and an `actors` map of authenticated subjects
to allowed actions. Assign `inspect` independently from `reserve`,
`claim` and `reconcile`. The authenticated installation supplies the
subject. The worker also verifies the recorded human decision, current
source and recipient entitlement.

To withdraw, retain an owner-controlled
`handoff-permission-revoked-<digest>.json` marker beside the grant. Its digest
hashes `[tenant, workflow_ref, engagement, grant_ref]`. Restore the current
marker with the custody state before returning a backup to service. Retain
the historical handoff status through the custody process.

The private handoff readback takes the workflow explicitly selected in
Home and the frozen run, run revision and artifact identity from its
verified evidence. It also requires the viewer's current inspection
permission. An approval with no outbox entry returns its recorded time
and destination with delivery unset. An existing outbox entry returns
only the durable stage, check time and independently confirmed receipt
fields it holds. The reader performs no network or reconciliation call.

The hosted Zap still needs the authenticated fetch binding and versioned
workflow. The separate, explicitly authorized test activation and its
rollback retain their own evidence requirements.
