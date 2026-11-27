# Reviewer replacement through saved owner authority

This OPE-548 backend work consumes the actual OPE-426 owner-settings ledger. It is a
scoped software producer. The selectors below identify generated local coverage;
exact execution results belong in the commit-bound review receipt. They do not
qualify the downstream successor consumer or an installed integration.

The inherited OPE-426 backend supports the zero-cost budget path. Its positive
monetary-budget qualification remains open. Reviewer replacement does not expand
that budget authority or establish full OPE-426 acceptance.

An administrator binds the workflow owner and custodian separately. The workflow
owner signs an assignment of a permitted successor against the exact current
settings revision and frozen proposal evidence. The custodian signs the resulting
settings save. One actor may perform both steps only when both roles are explicitly
bound. Distinct actors use distinct role verification keys; possession of the
custodian key does not grant the workflow-owner role.

Assignment does not itself change release authority. Saving the replacement must
commit the new settings head, old-request fences and a reconciliation intent for
each affected proposal in one durable transaction. Release callbacks must read
this canonical authority under the same engagement lock used for their effect.
Projection into the approval reconciliation journal may be retried after a crash;
it cannot be the only fence. Unknown provider outcomes remain held before any new
request is issued.

The required lock order for the backend replacement save is approval serialization
lock, engagement writer, then mandate writer. The assignment and save repeat both
role/key/expiry checks inside their transaction. The assignment uses a different
signed purpose from the settings save. Changing a trusted role or key epoch makes
an old unconsumed assignment stale.

The successor's credential is a new review grant over an exact frozen object. It
does not revive the previous execution mandate: existing run, source and training
authorization paths continue to reject withdrawn execution authority. Evidence
consumers must separately check current data visibility and the successor grant's
exact object binding. A previously accepted or indeterminate provider send never
becomes a successor vote.

Historical evidence and decision receipts retain the original actor. A successor
inspects the exact current proof after the replacement is saved. No old vote or
inspection transfers to the new reviewer. An ineligible assignment is refused without mutating valid work. The separate
no-eligible-successor acceptance requires a durable hold and private owner action
from the actual authority-loss reconciliation consumer; that integration is open.

## Run from the portable kit

Use `python -I -S -B opentour-slack.pyz reviewer-assignment --help` for the
signed assignment commands and `python -I -S -B opentour-slack.pyz owner-settings --help`
for the custodian preview and save. Work from the owner
worker with the installed private authority file and the appropriate signed
input for each role. Follow the [reconciliation guide](pilot-approval-reconciliation.md)
for existing requests before any successor review is issued.

## Generated producer coverage

| Selectors | Observation |
| --- | --- |
| `test_actual_owner_assignment_preserves_settings_and_frozen_proof`, `test_real_dual_signature_save_records_one_fence_and_keeps_old_execution_denied` | Distinct actors or an explicitly dual-bound identity save one assignment/revision; actual old execution authority stays withdrawn. |
| `test_owner_and_custodian_keys_do_not_substitute_for_roles`, `test_alias_pem_encoding_cannot_make_same_key_two_roles` | Role, purpose and canonical-key separation; alternate PEM encodings do not imply distinct keys. |
| `test_invalid_assignments_do_not_mutate_or_read_provider` | Foreign scope, stale epoch, expiry and invalid signatures do not create authority or read source rows. |
| `test_two_competing_assignments_cannot_create_two_current_replacements` | Competing signed settings saves admit one current head. |
| `test_replacement_of_inherited_proposal_keeps_every_generation`, `test_v1_run_is_explicitly_bound_after_v2_same_reviewer_renewal` | Inherited frozen proposals survive A → B → C → A and explicit v1-to-v2 adoption without reviving execution authority. |
| `test_same_reviewer_renewal_preserves_choice_with_a_fresh_generation` | An unchanged role/grant/source renewal retains the reviewer choice and old request inventory in a new inspection generation. |
| `test_failed_fence_transaction_preserves_previous_settings_and_mandate` | A crash before commit rolls back the settings head, withdrawal, fence and intent together. |
| `test_real_old_callback_after_saved_replacement_is_denied`, `test_replacement_commit_wins_before_old_confirm`, `test_old_confirm_commits_before_replacement_preserves_original_actor` | Actual old callback and both durable orderings preserve zero unauthorized effects and historical actors. |
| `test_two_actual_sealed_requests_preserve_unknown_remote_hold` | Both sealed request IDs and fingerprints survive one accepted and one indeterminate send; producer records unknown state and does not resend. |
| `test_completed_history_under_changed_source_does_not_block_current_assignment` | A genuinely completed old-source run does not block assignment of a later eligible worker artifact. |
| `test_v2_ordinary_renew_waits_for_serialized_actual_issue` | The shared approval lock orders a changing settings save after actual request sealing. This test supplies the OPE-414 outer wrapper explicitly because that consumer is on a separate integration branch. |
| `test_catalog_snapshot_refresh_preserves_frozen_transfer_and_renewal` | Routine catalog content refresh preserves A → B → C and same-reviewer renewal under unchanged source authority, retaining every frozen proof digest. |
| `test_v1_downgrade_renewal_serializes_and_keeps_existing_proposal_held` | A signed downgrade also waits for the approval writer; prior successor authority stays held and its request inventory remains durable. |
| `test_private_projection_ack_is_idempotent_and_grants_no_remote_proof` | Actual current operator scope and exact document digest govern private queue acknowledgment; ACK does not qualify a provider result or notification. |

The generated callback fixtures exercise the actual local consumer with a test
HMAC envelope. They do not run the Node signing action or contact a provider.

Still required: positive fresh successor inspection/confirmation and downstream
effects; generation-bound presentation metadata; OPE-414 verified multi-request
projection and unknown-state recovery; durable no-eligible-successor hold/owner
action and complete successor integration evidence.

## Consumer handoff

The producer must expose current saved authority for the exact organization,
engagement, run ID, run revision and artifact digest, including the settings
revision, reviewer grant epoch, original reviewer and current reviewer. Unknown or
out-of-scope proposals return a hold. A caller-supplied receipt alone cannot grant
authority: the consumer re-reads the current ledger and live administrator-bound
role state.

The coordinator owns `release_gate.py` and its check inside the effect lock. The
coordinator/sister owns `repair_workflow.managed_review_evidence`, which must return
the saved current reviewer without rewriting the original frozen evidence. The
existing Slack adapter consumes that backend value; no Slack UI changes are in
this lane. OPE-414 consumes the saved fence and successor binding before a new
request can be issued. Source and standing-mandate revocation checks remain in
force; replacing a reviewer does not authorize a new source read, fit or export.

Qualification must identify exact producer/consumer commits, generated fixtures,
test counts, denied effects, independent review, CI and protected integration.
Installed Slack/Zapier operations and customer proof are separate evidence.
