# Saved reviewer authority: consumer contract

This is the OPE-548 producer handoff, not evidence that the consumers below are
integrated. Installed Slack/Zapier and customer proof remain OPE-435. The inherited
OPE-426 positive monetary-budget, private UI and kit qualification limits remain.

`reviewer_assignment.current_review_authority(directory, organization, engagement,
run_id, run_revision, artifact_sha)` checks the actual tenant/engagement and private
settings ledger. It returns `None` only if no replacement is recorded for that
proposal. An existing stale, held, foreign or changed replacement raises; consumers
must not catch that refusal and fall back to the original reviewer.

The returned private document contains:

| Field | Meaning |
| --- | --- |
| `generation` | The exact current signed settings revision that authorized this review. |
| `reviewer_grant_revision` | Current administrator-bound reviewer identity and exact private release scope. |
| `reviewer`, `original_reviewer` | Current release reviewer and preserved historical actor. |
| `release_review` | Current reviewer/team/channel/audience/provider binding. |
| `run_id`, `run_revision`, `artifact_sha` | Exact frozen proposal identity. |
| `review_digest`, `plan_revision`, `intake_revision`, `dataset_revision` | Original frozen evidence bindings; replacement does not rewrite their source. |
| `assignment_revision`, `workflow_owner_role_revision`, `custodian` | Saved owner choice and separate custodian save provenance. |
| `state` | `fresh_successor_inspection_required` for a current review grant; a held generation cannot authorize an effect. |

Owner assignment and custodian save use separate signed purpose schemas. Distinct
actors must have different canonical public keys, not merely differently encoded
PEM files. One identity may use both roles only when each is explicitly bound.
The new owner key fields are accepted only in authority v2; v1 does not imply owner
assignment authority from possession of a custodian key.

Every v2 changing settings save, including ordinary renewals, takes the approval
serialization lock before the engagement and
mandate writers. The new settings head, withdrawal of prior execution authority,
immutable replacement generation and one reconciliation intent per affected
proposal share a mandate transaction. Approval-journal/file projections are not
atomic with that transaction: they must be idempotent and recoverable. The saved
generation is the canonical fence throughout recovery.

## Coordinator: release and evidence

`repair_workflow.managed_review_evidence` must resolve the saved grant while
preserving current data visibility and frozen object verification. The new grant
authorizes review of the exact frozen object; it does not revive the previous
execution mandate. Existing execution, source and training authorization paths
must continue to reject withdrawn authority. Copy the current `release_review`,
`generation` and `reviewer_grant_revision` into the returned private evidence
context without changing historical files or original digest values.

`release_gate` must compare the current grant with the sealed inspection and
record its generation in the durable decision receipt. Every vote and downstream
effect must acquire the existing engagement writer, re-read current authority and
hold that writer through the durable effect/receipt commit. A check performed only
before the lock does not establish ordering with replacement.

## Sister: private presentation metadata

`slack_adapter.Workbench._managed` currently copies only four fields into
`recorded`. Explicitly carry `generation` and `reviewer_grant_revision` into the
target revision and sealed presentation snapshot. Returning A → B → A must create
a new inspection generation even though actor, run and artifact are unchanged.
Changing wallclock timestamps alone is insufficient.

## OPE-414: request and reconciliation

Bind the saved generation and reviewer grant to the private sealed request and
its nonce. The provider envelope already binds request ID, nonce, installed Zap
revision and decision; no private proof or extra customer text needs to be sent.
On callback, retry and dispatch, compare the stored grant/generation with current
authority before acting. An old valid signature cannot authorize a new generation.

Read the durable `reviewer_reconciliation` intents scoped to the same ledger and
project the proposal fence to every matching old request. Preserve completed
decisions and their original actor. Do not resend an admitted request whose remote
result is unknown; obtain authenticated reconciliation before successor issuance.
A lost projection response or restart must not create another intent or vote.

## Private reconciliation API

Construct `ReviewerAssignments(OwnerSettings(directory, organization, engagement,
authority_file=trusted_owner_local_path))`. The installed bridge may accept that
explicit local path plus an authenticated actor. The actor must be the current
administrator-bound custodian or operator, and the authority document digest must
match the actual saved settings head. A caller role string or supplied receipt is
not authority. Replacement documents retain the authority basename relative to
the owner directory; key files remain private and administrator provisioned.

`pending_reconciliations(actor=actor, limit=50)` returns `pending_count` and bounded
`records`: each has `id`, immutable `document` and `document_sha256`. All reads are
scoped to the settings ledger's organization/engagement. The document's
`expired_request_ids` and `request_fingerprints` are private inventory references,
not authenticated provider proof. The consumer must verify each actual sealed
request against its policy/key and exact frozen proposal. It must independently
read the local admitted/transport/provider-status ledger. The captured
`remote_reconciliation_required` flag cannot clear a later unknown remote result.

After durably projecting every old request marker and its local journal state,
call `acknowledge_reconciliation(id, document_sha256, actor=actor)`. ACK is a
trusted local operator acknowledgment, not independent projection readback.
The consumer must enforce that ordering. Repeating an ACK returns the same private
receipt. It records `state=projected`, `provider_outcome=not_qualified` and
`owner_notification=not_qualified`; it grants no send, vote or notification proof.

Keep the outer approval lock through projection/ACK, but finish the approval
journal's SQLite transaction before ACK acquires training/mandate writers. A crash
after the canonical settings fence but before projection leaves the intent
pending. A crash after projection but before ACK repeats idempotent projection.
Historical intents can fence/ACK old requests; only a current document whose exact
generation, reviewer and grant match `current_review_authority` may refresh the
installation. Unknown remote state remains held regardless of ACK.

## Integration oracles still required

- Replacement committed first: old callback, old inspection and old release effect
  produce zero new approval/export; crash before projection has the same result.
- Old confirmation committed first: keep its original actor/receipt, and reject
  the now-stale replacement inventory for a fresh owner preview.
- Successor opens the exact current proof after save, then confirms once; duplicate
  save/confirm returns one logical revision/receipt. Test A → B → C and A → B → A.
- Same-reviewer renewal with unchanged roles, grant and source scope carries the
  reviewer choice into a new generation and requires fresh inspection. Changed
  scope remains held pending explicit reauthorization; owner-action delivery is
  a separate consumer gate.
- No eligible successor stays on hold with an authorized hold save; a malformed
  preview cannot invalidate valid work. Exercise multiple outstanding proposals,
  multiple old nonces, unknown sends and denied cross-tenant effects.

Generated producer tests and existing old-callback tests do not establish the
positive successor presentation/confirm or installed reconciliation path.
