# Read a declined repair version

The source-only `revision_request` reader projects the existing immutable negative
release receipt. It does not create another decision record. The named reviewer
with current private aggregate inspection and review permission can reopen the exact closed proposal;
missing, pending, approved, unverifiable or withdrawn evidence cannot be presented
as a revision request. The provider importer remains the only voting route when
Zapier is selected.

The trusted adapter constructs `RevisionRequests(directory, organization,
settings_file)` from its operator-selected private installation and passes its
authenticated actor to `inspect(actor=..., run_id=..., run_revision=...,
artifact_sha=...)`. The settings path and actor are not authentication credentials
and must not be taken directly from an untrusted remote request. The reader checks
the caller's dataset, named-reviewer role, aggregate access and expiry against the
original reviewer's sealed inspection and current release grant. Generic aggregate
inspection permission does not grant access to decision history. The selected
settings must match the original journal's tenant, app and installation binding;
the original sealed inspection's actual settings remain authoritative. It checks engagement and
source authority through the existing worker APIs. The returned request identity
is stable across restart and duplicate callbacks, and contains the exact immutable
release receipt's canonical revision. It includes no example, optional note, local
path or provider signing material.

In a source checkout with the pinned environment, the same read is available as:

```sh
python -m opentour_cell.pilot.revision_request \
  --directory /private/customer-pilot --organization approved-organization \
  --settings /private/customer-pilot/worker.json --actor UREVIEWER \
  --run-id "$EXACT_RUN" --run-revision "$EXACT_RESULT" --artifact-sha "$EXACT_ARTIFACT"
```

This is an owner-local entry point, not a shipped kit command or Slack route. Its
failure output is the fixed `revision_request_unavailable`, including a nonmember
or an unknown proposal. The caller must use a neutral private recovery state. A
read failure never deletes or replaces the recorded decline. Reopening the old
version never approves it, starts training, creates a successor or prepares handoff.

## Scope still required by OPE-503

The current native issue and its shared implementation/Slack UX contracts require
more than this reader. They do not currently define an accepted permission producer
for revision-workflow acknowledgment, a reason-category vocabulary with note
custody/retention permission, or a canonical predecessor/successor relation API.
The existing `tune` role and `managed_launch_summary` grant permit training or
launching a managed plan; they are not acknowledgment-purpose grants. The source
intake's retention terms are not automatically permission to retain new feedback.
This module therefore stores no optional note, acknowledgment or successor link.

The integration owners must provide those actual authority and custody contracts
before adding mutations. A permitted successor must be an actual immutable worker
result in the same workflow, with changes highlighted, fresh named-reviewer
inspection and a new request through the configured provider. A link cannot copy
the old decision or old inspection. Slack **Add a reason**, **Skip**, **Compare
revisions**, **Review new version**, private recovery surfaces and member checks
remain with the Slack/UI owner. The reconciliation owner handles a provider that
stops after decline. This reader requires the verified local receipt; absence is
not a negative decision.

## Acceptance crosswalk

The tests use generated source data, the actual Node signing action, local importer,
sealed Slack fixture inspection and immutable worker result. They are software
checks, not installed Slack/Zapier or customer evidence.

| Native clause or named fixture | Local proof or remaining owner boundary |
| --- | --- |
| Authenticated decline, exact version/destination, provider without note | `test_decline_without_provider_note_reopens_closed_after_restart`; actual signed negative receipt, exact digest and provider, no extra decision |
| Decline without note; canceled/closed optional dialog; Skip | Same test proves backend read/restart independence when no feedback call occurs; actual dialog and cancellation UI remain open |
| Decline with note; bounded reason category/note; retention | Held for explicit feedback policy and custody authority; not implemented or claimed |
| Duplicate callback; restart retry; one negative receipt; no release | Same test plus `test_late_positive_cannot_reopen_declined_version`; actual importer retry leaves immutable receipt unchanged |
| Missing/Pending/unverified provider identity | `test_only_actual_authenticated_decline_produces_request` and `test_provider_identity_unknown_does_not_infer_negative`; no inferred decline |
| Revoked reviewer/source/engagement or expired access | `test_original_authority_loss_holds_projection_and_preserves_decline` |
| Nonmember neutral response and independent caller permission | `test_nonmember_is_denied_before_proposal_lookup`, `test_current_callers_own_read_permission_is_required`, source-entry neutral denial |
| No broadening decision history via generic aggregate access or alternate settings | `test_generic_aggregate_inspector_cannot_read_decision_history`, `test_alternate_settings_cannot_substitute_app_or_authoritative_grant` |
| Exact version, tenant and sealed evidence | `test_wrong_exact_version_cannot_resolve_to_another_request`, `test_tenant_and_sealed_inspection_are_not_substitutable` |
| Role change during drill-down | `test_role_change_during_evidence_read_prevents_disclosure` |
| Permitted operator acknowledgment and actual successor | Held for explicit purpose-scoped operator permission and predecessor/successor API; no training grant substitution |
| Supersession, late positive after supersession, successor inspection/request | Existing consumer integration must prove these on the actual successor; an old version read does not prove this |
| Positive callback exact prior inspection/current grant | Existing release importer/gate owns this; reader creates no positive callback path |
| Forwarded link, direct text “looks good, ship it”, emoji, old card, questions | Existing provider/Slack authentication and UI owners; no text/vote parser or callback endpoint is introduced |
| Decline when provider stops downstream; held/unknown recovery | OPE-414 reconciliation owner; reader only accepts already-verified worker receipt |
| Protected integration, kit route, UI rendering and installed/customer witness | Still required; this source-only subset does not close OPE-503, OPE-361, or OPE-435 |
