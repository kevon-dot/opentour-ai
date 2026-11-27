# Private owner map review: OPE-538 backend

This backend consumes the unchanged OPE-512 guided source and branch semantics.
It keeps the registered starter owner, delegated intake actor, and authenticated
Slack editor separate. A private selection chooses the existing workflow,
starter selection, policy and review grant; it grants no permission.

The current custodian separately seals review permission with the existing
private starter-intake key. The capability binds tenant, engagement, workflow,
policy resource/revision, current starter-grant seal, registered owner/custodian,
decision schema, expiry, and explicit authenticated editor/read identities.
A withdrawal tombstone defeats restoration of an older signed capability.
Editors do not gain the starter owner's identity or any evaluator/trainer rights.

Before-code acceptance mapping from current OPE-538, OPE-512, OPE-513/OPE-539 and
the mandatory PRD/shared implementation contract:

| Check | Native requirement | Required local evidence |
| --- | --- | --- |
| M1 | Current actor-bound edit/read permission | Actual starter grant plus distinct signed review capability; foreign tenant, unknown actor, read-only edit, expired/withdrawn capability, actor rebind and stale starter grant deny |
| M2 | Inspectable cited branch table | Current source spans, input availability, typed conditions/action, fallback, source support and owner disposition; no model credentials |
| M3 | Five-family policy semantics | REVIEW/REVIEW/APPROVE/MANUAL/MANUAL with exact source spans; wrong inequality is an independent red control |
| M4 | Confirm, correct, leave unresolved | Explicit editor action/reason/time; typed action/condition validation; unresolved clauses and unsupported actions cannot become scored truth |
| M5 | Equal-authority conflict | One precedence question, both citations, unresolved affected branches; explicit total precedence with owner reason resolves the intended fixture |
| M6 | Concurrent stale save and retry | A changes revision, B receives current semantic diff; explicit reapply uses new revision; same operation/intention returns same receipt; changed intent conflicts |
| M7 | Source successor | New source/grant opens successor, preserves old artifact and marks dependent branch changes; old confirmations and stale patches never transfer silently |
| M8 | Disclosure and navigation | Metadata-only Home entry reads no policy; explicit view/citation and final disclosure recheck current actor, grants, source and revision; no unsafe historical source text in stale diff |
| M9 | Starter handoff | Only a real current confirmed map is eligible for OPE-513; draft/charter or unresolved authority is held. Case labels and frozen plan need their own actual confirmations |
| M10 | Editable trigger and fallback | Explicit named-editor assertion, reason and revision; current permitted fallback action or unresolved; stale settings diff/reapply, source successor invalidation and no implied dispatch |

The Slack action IDs, forms, accessible table, stale reapply controls and fixture
submissions belong to the assigned UI implementation. Integration and distinct
exact-commit review remain mandatory. Generated role/source fixtures prove only
software behavior. Provider/customer evidence and the OPE-539 scoring witness
remain separate; this module performs no inference, fit, release or live send.

After a benchmark freeze, a map/policy/label change requires an authoritative
successor benchmark intent. Reusing an old intake run cannot replace a frozen
definition. Until that successor producer is connected, the handoff stays held.

The installed entry is `open_map_review(directory, tenant, workflow_ref=None)`.
`entry` checks current navigation permission without reading policy bytes or
creating a journal. `preview` and `citation` read current permitted content;
`disclosure` holds and rechecks that permission before a caller publishes it.
`save` binds the authenticated actor, current revision and operation identity.
A stale exception carries a freshly authorized semantic diff. The private
register preserves every saved version and checks bodies, heads and operation
references before projecting them. It is not an external rollback-proof backup.

The selection contains only `schema_version`, `workflow_ref`,
`starter_selection_file`, `policy_file` and `grant_file`. The separately sealed
grant declares explicit editor-to-person mappings, read identities, permitted
decision fields and actions, along with the exact source/owner/workflow scope.
It must be issued by the actual current custodian; editing the selector cannot
create a grant. No wire role is used as authority. An unspecified trigger is
shown as unspecified. A `MapSettingsEdit` records a bounded trigger, optional
permitted fallback action and reason under the authenticated editor's identity.
Absent fallback remains unresolved. The fallback applies only when no supported
rule matches and all operative branches have been reviewed; it cannot turn an
unknown clause into scored truth. Its provenance is an owner assertion, never a
fabricated source citation. These settings grant no invocation or dispatch right
and require explicit reapplication after a source successor. Source priority has
its own citation; an asserted priority
records the named actor and reason. Duplicate semantic branch identities hold
instead of allowing an ambiguous citation or edit.

On a source successor, inherited confirmations are invalidated if a changed
overlapping rule or priority can alter their decision domain. Merely moving a
disjoint rule does not invalidate it. Unsupported operative spans remain visible
until explicitly left unresolved, excluded with a reason, or corrected into a
typed owner assertion. Such assertions remain distinct from source support.

This scope provides the actual current confirmed map and action projection.
It does not yet install the OPE-513 case-confirmation adapter or a frozen plan
authority. A map editor is not automatically the case owner or an evaluator.
Those connections, Slack integration and exact independent review are remaining
gates; neither the generated fixture nor a charter grants those rights.

An installation with several admitted workflows may provide the private
`map-inspection-catalog.json` with schema version
`opentour.pilot.map-inspection-catalog.v1` and a `selections` list of 1–12
distinct private JSON basenames. Each names an ordinary map review selection.
The catalog grants no access. Home resolves the current signed grants for the
authenticated viewer before offering a workflow, and checks them again on
selection and disclosure. A single-workflow installation can continue using
`map-review-selection.json`.

Each branch shows a generated decision-time example and its proposed action under
the current predicates and priority. Unresolved conflicts and shadowed branches
remain visible. Examples never enter a case pack or dispatch a decision.
The unit of work is an optional attributed map assertion. The verification
plan comes from the selected workflow's saved starter charter when the
authenticated actor has the original starter owner binding.

## Policy effective dates

A guided policy may declare one operative `Effective from YYYY-MM-DD.`
line. The map binds the exact source span and accepts a decision date when
evaluating that selected revision. A missing date or a date earlier than the
declaration holds the result. Invalid or competing date clauses stay
unresolved. An authorized source edit with a retroactive date requires a
new review. The prior receipt and proof keep their original source and
interpretation.
