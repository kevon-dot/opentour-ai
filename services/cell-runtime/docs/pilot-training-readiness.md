# Current live training readiness

OPE-542 reads the OPE-514 durable live reducer, records one immutable readiness
transition for each evidence/policy/mandate verdict, and provides a read-only
projection for private Home and questions. It never reserves, fits, freezes a
benchmark, stages approval or dispatches a release.

The worker constructs `LiveReadinessSource(live)` and `TrainingReadiness(directory,
organization, engagement, source=source, assessments=assessments)`. A worker
completion calls `LocalRepairAssessments.refresh(snapshot)` when a reviewed local
repair policy is installed, then `TrainingReadiness.refresh(workflow, actor=actor)`.
The UI calls only `read(workflow, actor=actor, expected_revision=...)`; it must
match the returned dataset and revision to its explicit authorized selection.
It may call this under an existing read-only engagement guard. Missing source,
absent receipt, changed evidence, changed policy or invalid authority is unavailable,
not an invented zero. Re-reading never performs a source context read or a repair.

The private source snapshot supplies organization, current engagement/workflow,
dataset binding, evidence revision and verified root references. Authenticated
current grants determine `can_bind_verifier` and the next actor. The projection
does not accept role flags or counters from a prompt or callback. No decisions
shows `no_live_decisions` without an error rate. A pending decision distinguishes
`no_verifier_bound` from `awaiting_outcome`; a reader without the custodian role
gets `ask_data_custodian_to_bind_verifier`. The renderer owns the actual action.

The worker-only `LocalRepairAssessments` uses a mode-0600 owner-pinned policy and
the existing `simple_repair.assess_development`, on verified development families
only. A `DecisionContext` implementation must resolve the exact observed input
reference under current authority and return only allowed decision-time fields.
Missing/reconstructed input remains unavailable. Protected families are never
passed to that context port. Duplicate corrections and synthetic context siblings
cannot add reducer roots. Every residual root must be a current verified root;
distinct groups, rather than repeated decisions on one family, determine recurrence.

The private policy schema is `opentour.pilot.live-repair-policy.v1`. Required keys
are organization, workflow_ref, engagement, owner_ref, dataset_ref/revision,
feature, allowed_features, labels, rule_mapping, fallback, terms,
development_families, protected_families, target_ref, mandate_ref/revision and
agreement_revision. Terms explicitly name minimum_residual_errors,
minimum_development_cases, minimum_coverage_bps and error_ceiling_bps. There is
no default recurrence threshold. Nullable target/mandate fields permit a legitimate
held result; they never constitute permission. An actual existing LearningMandates
record must match the workflow, owner, agreement and exact revision before the
adapter reports its rights/budget state. Its observed reserved fits/seconds remain
in the private assessment receipt. No reservation is created.

The current adapter always retains `untouched_holdout_required` and
`qualified_model_required`: a family list or model name does not prove those
independent qualifications. It also holds absent/revoked/expired mandate mappings.
These are explicit producer limitations, not asserted passing capability or
holdout evidence. OPE-506 and the applicable live holdout producer own those
qualifications. The source-context implementation, normal worker completion hook,
starter mandate mapping and Home wiring still need joined acceptance before
OPE-542 can close. The separate core port can represent an eligible receipt after
all actual producers exist; even that receipt cannot authorize dispatch.

Receipts retain root/family digests, actual counts, threshold, assessment/policy/
mandate/evidence revisions, augmentation `not_enabled`, reason and next owner.
Absent assessment/input produces an unknown residual count. The invoice fixture
with an observed input and independently verified correction produces 1 root,
0 residual groups against threshold 3, and `too_early_to_train`. Reopening truth
removes current credit and makes the prior receipt unavailable; history remains
private. Retry/restart and concurrent refreshes reuse the same receipt. Both
SQLite files use private permissions and reads use SQLite `mode=ro`.

## Reproduction

Use pinned Python 3.12.16 from a clean exact revision:

```sh
PYTHONPATH=services/cell-runtime/src .venv/bin/python -m pytest -q \
  services/cell-runtime/tests/pilot/test_training_readiness.py \
  services/cell-runtime/tests/pilot/test_live_readiness.py
```

These are generated local software fixtures, including the actual live reducer
and simple-repair implementation. They do not witness installed Slack, provider
traffic, customer results, or a qualified training route. Keep those evidence
levels separate. No real source, provider or paid operation is required here.
