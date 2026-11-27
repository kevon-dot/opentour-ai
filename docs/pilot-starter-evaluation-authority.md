# Starter execution and retained inspection authority

This scoped OPE-539 adapter joins the actual owner-confirmed case receipt to the
scorer and private result reader. It neither changes the case grant nor creates
a new confirmation or freeze. Normal worker and Slack composition are separate
handoffs. Generated tests qualify this software, not a customer or real-model run.

The private `starter-evaluation-selection.json` uses schema
`opentour.pilot.starter-evaluation-selection.v1` and two bounded maps named
`executions` and `inspections`. Each maps an explicit execution reference to a
private JSON basename. Selection conveys no permission.

Each file contains `grant` and `seal_sha256`, an HMAC over the canonical grant
using the existing private starter-intake key. The execution schema is
`opentour.pilot.starter-execution-grant.v1`; inspection uses
`opentour.pilot.starter-inspection-grant.v1`. Both bind `grant_ref`, `tenant`,
`custodian_ref`, `case_authority_ref`, `case_envelope_digest`, `permission` and
`expires_at`. The permission is the exact `EvaluationPermission`, including
the original freeze and plan, evaluator, run, workflow, epoch, explicit execution,
model and baseline-grant revisions, comparator revision, permitted fields and
case bound. Execution permission revision equals its grant reference. Inspection
additionally binds `viewer_actor` and `owner_ref` to the current original setup
owner; its separate expiry governs retained access. Inference permission may have
expired or been withdrawn without invalidating otherwise permitted inspection.
The original case/source/map/owner scope must still be current. A withdrawal
tombstone named `starter-evaluation-withdrawn-<grant_ref>.json` fences that grant,
including a restored copy. No role string in a request authorizes an operation.

`StarterEvaluationAuthority(settings, authenticated_actor).work(execution_ref)`
resolves existing admission metadata only. Its `guard` and `revalidate` implement
the scorer port; `cases` exposes the existing input-only and late evaluator-target
projections. The evaluator must match the original frozen plan and receives no
owner editing rights or map UI role. The source authority's internal guard pins
the existing metadata; it does not relabel the caller as an owner. The original
case envelope, authority receipt, map settings and freeze identity remain intact.

`comparator_context(actor, work, permission)` is available only under the held
execution guard. Its canonical revision binds the context schema, comparator
identity, confirmed map/source/settings, ordered typed rules, field kinds and
explicit fallback. It contains no case answers. Unknown rules and date-dependent
execution without a separately pinned decision date hold. The durable comparator
consumes this port; a model registration and baseline permission remain separate.

`StarterInspectionAuthority` implements the result-reader port. Its `entry`
reads navigation metadata without policy bytes, case bodies, score journals,
admission writes or inference. Full inspection derives input and answer-population
digests from the validated original freeze and reservation ledger independently of
the evaluation journal. Both evaluator and inspection paths recompute the frozen
revision from its original canonical scope, case rows, plan and family split,
and compare it with the separately sealed permission. A restored private database
cannot substitute another answer population just by restoring its local hashes.
Inspection does not call model discovery, execution or lookup.

Acceptance mapping: actual owner confirmation and same-freeze evaluator access;
owner/editor/materializer denied implicit evaluation; explicit retry versus a new
sealed rerun; current case, source, epoch, map, actor, evaluation and retention
denials; grant loss before raw policy reads and before score commit; no Home file
effects; immutable original authority bytes; independent population readback;
actual durable comparator and scorer integration. The purchasing fixture exercises
five generated reserved families. It does not replace OPE-515's separate categorical
seven-family/four-reserved numeric oracle, installed UI journey or actual-model
witness. No fit, export or live quality claim is made.
