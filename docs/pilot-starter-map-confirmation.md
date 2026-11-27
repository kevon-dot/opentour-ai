# Starter case authority from an actual reviewed map

This adapter connects the existing starter case core to the current map-review
receipt. It does not infer case-owner or evaluation rights from map editor roles.

Before implementation, the current native OPE-513, OPE-538, OPE-539, OPE-505,
OPE-515 and the linked PRD/implementation contract map to these checks:

| Check | Required behavior | Positive and adverse evidence |
| --- | --- | --- |
| C1 | Actual owner identity | Current StarterSetup Slack-to-registered-owner binding plus independently sealed case capability; an ordinary map editor, replaced binding, foreign actor or expired grant cannot act |
| C2 | Actual current map | Current confirmed_ref, source hash, workflow and admitted run; a draft, changed source, stale map or superseded admission holds before cases are created |
| C3 | Explicit adjudication | Provider proposals retain suggestions; only authenticated original-owner actions supply labels/reasons; unresolved stays outside scored truth |
| C4 | Frozen settings | Separately sealed baseline/comparator/scorer/abstention plan, allowed actions and critical branches; missing freeze purpose holds, with no model credential required |
| C5 | Whole-family freeze | Reuse the existing core's duplicate, leakage, reservation and immutable manifest checks; current owner saves yield a frozen_unscored receipt and restart reads it |
| C6 | Durable attribution | Persist the exact sealed authority and authenticated-owner binding before a case mutation; retries retain the same core operation and do not fabricate completion |
| C7 | Withdrawal during Save | Recheck actual owner mapping, source/setup/case grants and map revision inside the case transaction before commit; revocation rolls back labels/splits |
| C8 | Worker separation | Only an explicit current worker materialize grant reads the frozen projection; no implicit evaluator, inference, fit or release rights |

The adapter is a new module and test; the independently reviewed case core and
map service are unchanged. Evaluation requires a separate future exact-freeze
capability plus OPE-539's execution authority. Case generation enters through an
explicit proposal interface and never auto-adjudicates suggestions. Slack forms
and the complete seven-family clean-install oracle remain separate integration
work until they invoke this entry through actual emitted controls.

`StarterCaseSession(settings, authenticated_actor)` is the private owner/worker
entry. `entry()` returns only a permitted workflow, run and opaque revision;
it reads neither policy bytes nor case targets. `propose(provider)` accepts
suggestions through the proposal interface. `review`, `adjudicate` and `freeze`
remain separate named-owner operations. Completed proposals are reused before
the provider is called again. An uncertain authoring-inference operation still
requires OPE-552's separate permission and recovery adapter; this scope uses a
deterministic generated provider and grants no inference right.

The non-authoritative `starter-case-selection.json` selects one private grant.
The sealed capability pins the original owner and authenticated Slack binding,
current intake/source/settings, confirmed map and explicit trigger/fallback
assertion, frozen plan, critical branches and purpose grants. Its immutable audit
receipt records the complete signed capability and the verified owner binding.
Changing a map or authority cannot create a second case pack on the same admitted
benchmark; it requires a real successor admission. A plain map editor, reader or
worker is never converted to the original owner.

The map and intake readers share one already-verified source-owner guard during
a case operation, with current binding and grant checks on reentry. Case writes
recheck that authority before their transaction commits. The actual benchmark
worker composes `BenchmarkIntake.materialize(..., session,
target_state="frozen_unscored")`; the normal intake layer releases its writer
guard before invoking the producer. It adopts only an existing owner freeze.

The local joined witness uses the five purchase-order families, an intentionally
wrong suggestion corrected by an actual owner action, a whole-family split and
normal benchmark publication with zero model jobs. It is not the seven-family
P1 clean-install oracle. That categorical map, its owner review controls and
the complete OPE-515 witness remain integration gates, as do separate evaluator
authority and scoring. Missing freeze/evaluate permission stays a hold.

The permitted correction-label list is distinct from required coverage. Each
confirmed branch declares its actual material action as a coverage cell, while
the owner can still correct a wrong suggestion to any current permitted label.
All five reviewed purchase-order families cover the three material branches;
excluding both missing-PO families leaves only REVIEW uncovered. The core's
optional coverage contract preserves the exact identities of earlier freezes
that did not declare separate coverage cells.

The private Slack Home offers **Review starter cases** to the current named
owner with the signed case-review purpose. **Generate guided cases** uses the
local guided authoring profile when proposal authority is present. The owner
can inspect each source passage, adjudicate each suggestion with a reason, or
leave it unresolved. After every case has a disposition, the owner assigns
whole families to development, reserved evaluation, or exclusion, reviews
reserved coverage and explicitly confirms the freeze. The frozen view shows
the receipt and aggregate support. Separate worker authority governs benchmark
publication and separate evaluation authority governs scoring.
