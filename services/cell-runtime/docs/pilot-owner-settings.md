# Private owner settings backend (OPE-426)

This rebuild uses the current `LearningMandates` ledger and authorized workplace selector. It provides an owner-local CLI and an API for the existing private setup page. It does not implement that page or install a provider connection. The historical `c17c3a8` implementation was unavailable and supplies no test credit.

## Authority and API

The hosting application must construct `OwnerSettings(directory, organization, engagement, authority_file=...)` from administrator-controlled configuration and pass its authenticated actor. Never accept those constructor bindings, a trust anchor, or an actor identity from an agent request. The authority JSON and public key are private, physical files owned by the local operator. The worker holds only a public verification key; the custodian signs outside the worker directory.

The strict authority document contains `schema_version` (`opentour.pilot.owner-settings-authority.v1`), `organization`, `engagement`, `custodian`, `operator`, `reviewers`, `workflow_owner`, `public_key_file`, `public_key_sha256`, `expires_at`, `revoked`, and `initial_mandate`. Custodian, operator and reviewer must be distinct. `workflow_owner` may explicitly equal the custodian. It grants read access only here; owner assignment and a separate owner signing key belong to OPE-548. `initial_mandate` binds the existing baseline when the first settings revision is saved. If any mandate already exists, an administrator must bind that baseline; omitting it cannot reset consumption.

`preview(policy, actor=..., action="save")` returns the exact prior receipt, changed mandate fields, and a claim to sign. Policy has exactly `mandate`, `agreement`, `connection_file`, and `currency`. The mandate uses the existing strict schema, including a fresh immutable identifier, explicit allowed fields/uses/model profile, cumulative caps, owner/operator/reviewer/payer and expiry. Currency is USD and the existing backend accepts only zero monetary spend. Rule-only policies have no training use/profile or fit/time budget. Paid model provisioning is unsupported.

The operating agreement retains the normal cycle worker's schema. The source connection must be locally installed and currently authorized. Preview validates grant expiry/revocation, tenant, source/field scope and independent correction authority, then reads only bounded resource metadata. It binds the exact connection, selected pair and grant revisions, as-of/split, retention reference, adjudicators and the existing intake exclusions. It does not read source rows, train, export or send messages. Model/tool arguments cannot authorize a save.

Preview and save both validate operating terms with the normal worker's existing protected-comparison validator before discovering source metadata or activating a successor. Unknown terms, invalid coverage/error bounds, malformed terms and duplicate slice names are refused while the saved policy remains active. A valid changed agreement still needs an exact-prior signed save. The comparison term `max_cost_per_1000` is a measurement ceiling; it does not implement a monetary training budget or charge settlement.

`save({"claim": ..., "signature": ...}, actor=...)` verifies a custodian RSA/SHA-256 signature over UTF-8 `canonical(claim)` (without a trailing newline). The signature is base64. Use a signer outside the worker to produce it; this module never accepts or stores a private key. The five-minute preview binds action, actor, tenant, engagement, prior revision, policy and live source authority. Unknown fields, wrong roles, foreign tenants, stale previews, changed grants and forged signatures are refused. The same saved claim returns the same receipt while its authorization remains valid.

Save rechecks authority/signature, live grant and cohort lease under the existing training-engagement/mandate writer transaction. It commits the old mandate's withdrawal, renewal link, successor mandate, immutable settings receipt and head together. Failed/competing saves roll back all of them. Renewal budget enforcement queries the original runs/reservations across the complete renewal lineage. Per-mandate historical counters remain unchanged; new `cumulative_*` fields report total usage. Failed, running and withdrawn reservations remain charged. Lowering a cap below usage prevents new admission/fits and never refunds prior consumption. The normal worker uses cumulative source counters before reading source content.

Receipts preserve `revision`, `prior_revision`, `mandate_id`, `mandate_revision`, `binding.grant_revision`, `authority_revision`, custodian `actor`, `workflow_owner`, `workflow_owner_role_revision`, `reviewer`, and `reviewer_grant_revision`. Role revisions are derived from the bound identities and authority; the reviewer grant also binds workflow and exact private review scope. The source grant and reviewer grant revisions are distinct. Changing an existing reviewer is held with `reviewer_replacement_settings_required` until OPE-548's genuine owner assignment, custodian save and proposal reconciliation consumer is integrated. No replacement vote or authority is fabricated.

## Pause, withdrawal and worker use

`preview(None, actor=..., action="pause" | "revoke")` requires a saved head but permits withdrawal even after the source grant expires. Its signed save durably denies the old mandate. `renew` requires a fresh mandate identifier and retains the same ledger lineage. Every policy save withdraws the prior mandate, so previously exported worker configs and delegated source/release receipts remain fenced. Restoring a previous settings file does not restore its mandate authority.

The stable receipt records old-artifact retention and required running-fit cancellation. Call `reconcile(receipt, actor=...)` after save to retry the existing local withdrawal markers for all old fit destinations; inspect its pending count. Durable denial commits first, so a crash cannot leave new work authorized. Reconciliation never refunds accounting or authorizes release. The current retention reference continues to govern private artifact retention; this API does not promise deletion.

`worker_config(receipt, actor=...)` accepts only the current active receipt and a custodian/operator. It checks current mandate/grant authority and writes immutable private agreement/config files. Its returned `config_file` is consumed directly by the existing entry:

```sh
python -I -S -B opentour-slack.pyz cycle-worker \
  --directory "$PRIVATE_DIRECTORY" --organization "$ORGANIZATION" \
  --config "$CONFIG_FILE_FROM_RECEIPT" --once
```

No mutable worker pointer is installed automatically. After saving, reconcile the old mandate, export the current config and configure the existing worker service to use that path. Old configs fail closed. To roll back policy values, preview and sign a new successor using the desired old values and a fresh mandate identifier; never rewind the settings database or restore an old grant to revive authority.

From the extracted kit, use `python -I -S -B opentour-slack.pyz owner-settings`
with `preview`, `save`, `status`, `worker` or `reconcile`. In a source checkout,
use `python -m opentour_cell.pilot.owner_settings` with the same arguments.
The CLI requires `--directory`, `--organization`, `--engagement`, `--authority`
and `--actor`. Use `--input` for private JSON, and select the preview action
with `--action`. Keep output in the private owner directory because it includes
actor identities and scope.

## Acceptance and limits

| Current native requirement | Executable evidence |
| --- | --- |
| Exact previous revision, candidate diff, signed CAS, stable worker receipt | `test_signed_preview_retry_roles_and_real_worker`, `test_competing_saves_and_explicit_prior_preview_atomicity` |
| Distinct custodian/reviewer/operator; no owner-only save | wrong-role parameter cases and `test_workflow_owner_binding_is_distinct_from_custodian_save` |
| Live grant/field/source/pair authority, independent verifier, expiry | live-change parameter cases; forged/foreign/expired/unknown packet cases; expired-grant pause |
| Pause/revoke/renew; stale worker/receipt denial; no source reads on denial | pause/revoke parameter cases, read-only policy test, lower-cap normal worker test |
| Atomic competing edits and rollback | concurrent save and injected successor failure tests; authority changed before writer lock test |
| Invalid operating terms cannot withdraw a valid policy or trigger source discovery; valid changes reach the worker | `test_invalid_operating_terms_cannot_replace_saved_policy` (four preview/direct-signed-save cases), `test_valid_changed_operating_terms_reach_normal_worker` (actual `cycle_worker` subprocess) |
| Cumulative fit/time/run/row enforcement, no reset/refund | renewal fit/seconds test plus full existing mandate suite; normal worker lower-cap check |
| Actual worker handoff | subprocess `cycle_worker --once`, real descriptor-backed generated MCP reader, pending private review; zero outbound network in container |
| Initial baseline cannot bypass reviewer replacement; waiting operator cannot export after removal | `test_first_settings_cannot_replace_existing_mandate_reviewer`, `test_worker_rechecks_waiting_operator_and_current_authority` |
| Lease remains held through commit; interrupted file publication recovers | `test_cohort_guard_remains_held_until_successor_commit`, `test_interrupted_worker_publication_has_no_partial_final_and_retries` |

Run the focused owner-settings, learning-mandate, autonomous-cycle and workplace-context tests under Python 3.12.16 on Linux. The workplace reader's verified descriptor launch is not currently macOS-qualified (existing issue #94); do not replace it with an unsafe pathname launch to make a local test green. Local Linux aarch64 generated fixtures are distinct from the required Linux x86-64 protected CI and from installed Slack/Zapier/customer qualification.

Remaining closure gates: independently reviewed exact published commit, protected integration/CI, and the software session's authenticated private-page binding and end-to-end UI evidence. The versioned kit includes the owner settings CLI for local operation. OPE-548 owns reviewer replacement integration; OPE-367/OPE-435 retain actual account/install/provider qualification. Generated local receipts earn no installed/customer acceptance.

Positive monetary-spend renewal and lowered monetary-cap behavior remain **unimplemented and unqualified**. The reused ledger rejects every nonzero `max_cost_cents`; these tests prove cumulative runs, rows, reserved fits and reserved seconds only. UI/kit integration must not be counted as general monetary-budget conformance. A future cost/model-connector handoff must define canonical reserved/actual charge receipts, currency and units, and atomic consumption across the same mandate lineage, followed by positive-charge carryover, lowered-cap and no-refund fixtures. This change introduces no substitute cost store and does not modify the coordinator's cost/model connector.
