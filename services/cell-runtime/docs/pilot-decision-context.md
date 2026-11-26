# Observed application context for readiness

The explicit live worker can assess simple repair using a customer-owned export
of the application’s observed decision-time inputs and committed classification.
This input is separate from later verifier truth. It does not assert a captured
model prompt, model response, protected evaluation result or training authority.

The optional `context_file` basename in `readiness-selection.json` selects a
private `opentour.pilot.decision-context-grant.v1` envelope. Selection is not
permission. The data custodian signs a separate bounded context grant with the
existing private live-authority key under the engagement guard. Its `grant` and
`seal_sha256` use the same canonical JSON/HMAC-SHA256 convention as live authority.
Keep the key and all source files private to the worker owner.

The grant binds `grant_ref`, current `authority_revision`, `tenant`, `workflow_ref`,
`engagement_epoch`, `worker_ref`, decision `resource`, `grant_revision`,
`source_revision`, `deciding_namespace`, `allowed_fields`, `expires_at`, and `body`.
The body pin contains a private JSON basename and the SHA-256 of its canonical
JSON. The worker must already hold current live intake and inspection rights.
Do not stage a context grant unless the custodian permits those exact observed
fields and that resource for readiness assessment. A durable private
`decision-context-withdrawn-GRANT_REF.json` tombstone denies that grant even if an
older signed file is restored. Other permission changes require a current signed
revision, never an edited source body under an old pin.

The body schema is `opentour.pilot.observed-decision-context.v1` with `records`.
Each record contains the exact `decision_id`, `decision_revision`, `case_id`,
`family_id`, `input_ref`, `action_ref`, `policy_ref`, `input_fidelity`, independent
source `receipt_ref`, permitted `features`, and the observed `incumbent`
classification. Only `observed` inputs matching the authoritative application
commit qualify. Record the incumbent as its actual `automatic` label, `review`
status, or null when classification was not observed. An opaque action reference
alone is insufficient to infer automatic behavior. Do not include later outcomes
in these features. Those come from the separately bound verifier resource.

On explicit `live-reader` materialization, the existing owner policy selects
development families and permitted features. The worker validates metadata before
opening the pinned body, checks exact decision/action/policy identity, and rechecks
current permission before publishing the assessment. Protected families are not
read for repair. Missing, reconstructed, stale or denied contexts cannot produce
a successful repair claim. No fit, reservation, release or benchmark change is
authorized by a context receipt.

Readiness records bind the context permission revision and observation receipt.
Opening Home reads only metadata and persisted assessments; it never reopens raw
context data. Revocation or replacement invalidates an old readiness verdict.
An already authorized historical assessment can remain readable if its raw export
is moved after successful materialization. The next worker refresh needs the
original pinned input again and holds if it is unavailable. Account for context
grants, observation bodies, withdrawal tombstones and backups in the custodian’s
private closeout inventory; this adapter does not delete them.

Generated tests use real private selection, current signed grants, source intake,
worker commands, simple repair and Home. The one-correction fixture reports one
verified root, zero residual groups and a threshold of three. Restart reuses the
verdict; reopen removes the verified root. Customer deployment and real observed
source provenance still require their own evidence.
