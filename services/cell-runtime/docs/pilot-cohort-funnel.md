# Private cohort funnel

The cohort operator publishes one private funnel report for the first-ten cohort. It joins the
organizations admitted in the authoritative qualification and lease register
([private pilot delivery lease](pilot-cohort-lease.md)) to the private stage receipts, and it
reconciles every count against the evidence behind it. A second, evidence-free weekly view
carries only counts for team-wide use.

The unit is one distinct canonical `organization_ref`. A display-name change, a second CRM
record, a repeated run, a second workflow or a fresh engagement epoch for the same organization
never adds an organization to a column.

What the funnel never does:

* write the register or a receipt;
* infer one stage from another. Payment never comes from an approval, an owner decision, a
  delivery or a fee waiver. Acceptance never comes from a starter readout, a fit or enrollment.
  A CRM stage, a Slack action, a meeting, a proposal or a generated fixture credits nothing;
* count one organization twice in a column;
* report a missing value as zero. Unknown costs, durations, fit counts and limit flags stay
  unknown and are counted separately.

## Evidence tier

The report is `digest_self_consistent_only` and states `issuer_authenticated: false`. It
re-checks no key. Register rows were HMAC-checked at admission by the party that holds the
keys (`locally_keyed`); reading them back re-checks nothing. Each stage receipt states its own
`evidence_tier`. The funnel caps that claim at `locally_keyed`, because no signing or custody
contract exists yet (OPE-566), and counts the receipts whose claim it capped.

Following the owner decision recorded in the custody PRD (OD-1, 2026-11-27), the headline
splits the internal acceptance count by tier. Only `customer_witnessed` acceptances are
externally claimable, so `externally_claimable_accepted` stays 0 until a verifier exists. A
report is computed for exactly one `setup_kind` (`generated`, `approved_test_installation` or
`real_customer`). Receipts of another kind are refused and listed. Only a `real_customer` report
sets `customer_credit: true`; a generated rehearsal proves software behavior only.

## Columns

Field map `opentour.pilot.cohort-funnel-field-map.v1` defines every column. Its digest is pinned
in the code: editing the map without publishing a new version refuses every report. Each column
is credited only by its own source and lists the distinct organizations and the evidence
references behind its count.

### `qualified`

Credited by a stored qualification receipt with disposition `qualified` in the register's
`qualification_event` table. Discovery-only and lost events credit nothing. Owner: intake owner.

### `enrolled`

Credited by a live lease row in `cohort_lease` for an admitted engagement. Enrollment earns no
acceptance. Owner: cohort operator.

### `ready_to_result`

Credited by a `ready_to_result` receipt. Its `effective_at` is the instant permissions, inputs,
worker and policy were all ready. Record measured customer wait and operator setup seconds when
known; otherwise leave them null. Owner: intake owner.

### `starter_readout`

Credited by a `starter_readout` receipt for a frozen starter readout. An unscored starter
yields starter readout 1 and real-case evaluation, exact fit, delivery and payment 0.
Owner: cohort operator.

### `real_case_evaluation`

Credited by a `real_case_evaluation` receipt: the authorized recipient's recorded rerun or
witness entry for a comparison on real permitted cases. That entry is a human attestation in the
operator's own register, `locally_keyed` at most. A later rule-only real-case comparison for a
starter organization adds one real-case evaluation and no fit. OPE-357 owns the real-case denominator.
Owner: authorized recipient.

### `exact_fit`

Credited by an `exact_fit` receipt binding the completed fit job and artifact digest. A
comparison of an exact model credits no fit. Owner: ML owner.

### `owner_decision`

Credited by an `owner_decision` receipt: a dated decision of `continue`, `revise`,
`gather_evidence`, `stop` or `defer` bound to a credited readout revision of the same engagement.
A decision naming an unknown readout credits nothing. Owner: workflow owner.

### `accepted`

Derived, never recorded directly. An organization is accepted once when the owner's latest
decision on a readout revision is `continue`, `revise`, `gather_evidence` or `stop`, and that
decision was made at or after a credited real-case evaluation of the same revision and
engagement. A `stop` accepted this way is a qualifying stop under OPE-357's receipt rules. A stop
on a starter readout, a deferral, silence or a pre-readout withdrawal earns zero. The report
splits accepted organizations by the outcome of their latest accepting decision and by evidence
tier. Owner: cohort operator.

### `delivered`

Credited by a `delivery` receipt: a destination receipt for the exact artifact digest. An
approval or an owner decision credits no delivery. Owner: authorized recipient.

### `paid_recurring`

Credited only by a `payment` receipt that binds a continuation agreement and actual recurring
payment evidence. Approvals, owner decisions, deliveries, proposals and fee waivers never credit
payment. The receipt carries no amount or commercial term. Owner: account owner.

### `fee_waiver_proposed`

Credited by a `fee_waiver` receipt with status `proposed`. It stays in its own column and never
counts as paid. Owner: account owner.

### `fee_waiver_agreed`

Credited by a `fee_waiver` receipt with status `agreed`. It stays in its own column and never
counts as paid. Owner: account owner.

## Current states

States describe the register at the report's `as_of` time, not milestones reached.

| State | Meaning |
| --- | --- |
| `queued` | A waiter with no lease for its engagement, unexpired, whose qualification is still current. |
| `queue_stale` | A waiter that expired or whose qualification changed. It needs a fresh keyed acquisition command. |
| `active` | The open lease (terminal kind still empty). |
| `active_requires_reconciliation` | The open lease expired or lost its qualification. Reconcile and close it before capacity moves. |
| `closed` | An engagement with a keyed closeout command. |
| `withdrawn` | An engagement with a keyed withdrawal command. |
| `lost` | The organization's latest qualification event is `lost`. |
| `deferred` | The organization's latest credited owner decision is `defer`. |

Drop-off reasons use closed codes only: `qualification_lost`, `engagement_withdrawn`,
`queue_stale`, `owner_stop` and `owner_deferred`. Owner stop reason references stay in the
private report.

## Stage receipts

Each stage event has one immutable receipt with schema `opentour.pilot.cohort-stage-receipt.v1`.
A batch file has `schema_version` `opentour.pilot.cohort-stage-receipt-batch.v1` and `receipts`.
Every receipt has exactly these fields: `schema_version`, `receipt_id`, `revision`,
`supersedes_revision`, `stage`, `organization_ref`, `engagement_revision`, `recorded_at`,
`effective_at`, `evidence_digest`, `evidence_tier`, `setup_kind`, `source_record_ref` and
`facts`. References are opaque. Never put a person's or organization's name, an amount or a
commercial term in a receipt.

* `revision` starts at 1. Revision `n` must name `supersedes_revision` `n - 1`. The highest
  revision with a complete chain is current. A revision above a gap is held.
* Two different receipts claiming the same `receipt_id` and `revision` hold that receipt
  entirely. An exact repeat delivery is counted once and reported as a duplicate delivery.
* `effective_at` (the event instant) must not be later than `recorded_at`. A receipt recorded
  after the report's `as_of` is left out of that report.
* `source_record_ref` names the CRM or private record the receipt came from. Several records for
  one organization collapse into one organization.
* A receipt credits an organization only when the register admitted that organization's
  engagement as `qualified`. Otherwise it is listed as `receipt_not_admitted`.
* A `correction` receipt marks one exact revision `disputed` or `reversed`. While that revision is
  current it credits nothing, and only the columns it credited lose the organization. A valid
  superseding revision reinstates the credit. A correction of an already superseded revision is
  history and moves nothing.

## Stage receipt facts

`facts` holds exactly the listed fields. A field marked optional may be null for unknown.

| Stage | Facts |
| --- | --- |
| `ready_to_result` | `customer_wait_seconds`, `operator_setup_seconds` (both optional) |
| `starter_readout` | `readout_revision_ref`, `benchmark_state`, `compute_seconds`, `limit_exceeded`, `cost` |
| `real_case_evaluation` | `readout_revision_ref`, `population`, `comparison`, `witness_ref`, `compute_seconds`, `fit_count`, `limit_exceeded`, `cost` |
| `exact_fit` | `fit_job_ref`, `artifact_digest` |
| `owner_decision` | `readout_revision_ref`, `decided_on`, `owner_ref`, `outcome`, `stop_reason_ref` |
| `delivery` | `artifact_digest`, `destination_receipt_ref` |
| `payment` | `continuation_ref`, `payment_evidence_ref`, `recurring` |
| `fee_waiver` | `status`, `terms_ref` |
| `correction` | `target_receipt_id`, `target_revision`, `kind`, `reason_ref` |

`benchmark_state` is `frozen_unscored` or `scored`. `population` is `real_permitted`.
`comparison` is `rule_only`, `incumbent_vs_repair` or `exact_model`. `recurring` must be `true`.
`stop_reason_ref` is required for `stop` and null otherwise. `decided_on` is the owner's date and
may be at most one day after the UTC date of `recorded_at`. A `cost` is null or an object with a
decimal-string `amount`, a three-letter `currency` and a `basis` of `observed` or `modeled`.
`compute_seconds`, `fit_count` and `limit_exceeded` are optional.

## Timings, runs and costs

* Onboarding wait runs from the first `qualified` observation of the engagement to its first
  `ready_to_result` receipt. Customer wait and operator setup come only from the measured values
  on that receipt.
* Ready-to-first-result runs from readiness to the first starter or real-case readout, and
  separately to the first real-case evaluation. Compute time is the measured
  `compute_seconds` of that first readout. Durations report the population, known and unknown
  counts, the median and the maximum.
* Runs exceeding the customer's limit and zero-fit evaluations are counted among receipts whose
  flag or fit count is known; unknowns are counted separately.
* Cost per accepted pilot uses the runs of each accepted engagement. An organization's cost is
  known only when every such run carries a cost of the same basis and currency. The report shows
  observed and modeled costs separately, the number of accepted organizations with a known cost,
  and the total divided by that number. Different currencies are not added.

## Outcome roll-up

The outcome roll-up is withheld unless the owner permits outcome disclosure for this report. It
never withholds the base funnel. It consumes:

* one OPE-522 readiness cohort receipt with schema `opentour.pilot.readiness-cohort-receipt.v1`:
  `schema_version`, `setup_kind`, `readout` (the `opentour.pilot.readiness-cohort.v1` readout
  fields) and `revision`, the digest of `readout`. Counts are workflows, not organizations. The
  current dispositions must add up to the workflow count, and the pending ages must match the
  held, pending and disputed workflows. Otherwise the roll-up is held as inconsistent.
* one OPE-517 `opentour.tuning-benefit-result.v1` result and one `opentour.label-efficiency.v1`
  record of the report's own evidence kind. An absent, unrun or inconclusive comparison shows
  "Augmentation benefit unproven". An absent label record shows "Label savings not measured".
  Extra expert minutes on the augmented route are shown as additional burden.

A missing or inconsistent input holds only its own part and names the gate it waits on. One
report takes one readiness receipt; separate cutoffs and windows are separate reports, and the
funnel never averages percentages across them.

## Mismatch codes

Every mismatch names the role that resolves it. Refused receipts are listed with the same codes.

| Code | Owner role | Action |
| --- | --- | --- |
| `receipt_not_canonical_json` | `cohort_operator` | Re-export the receipt as plain JSON. |
| `receipt_schema_unknown` | `cohort_operator` | Use the current stage receipt schema. |
| `receipt_fields_invalid` | `cohort_operator` | Supply exactly the listed fields and facts. |
| `receipt_value_invalid` | `cohort_operator` | Correct the reference, revision, digest, vocabulary or setup kind. |
| `receipt_time_invalid` | `cohort_operator` | Correct `effective_at`, `recorded_at` or `decided_on`. |
| `receipt_tier_unknown` | `cohort_operator` | Use a tier name from the custody PRD ladder. |
| `receipt_outside_setup_scope` | `cohort_operator` | Publish it in a report of its own setup kind. |
| `receipt_revision_conflict` | `cohort_operator` | Resolve which content is authoritative and issue a new revision. |
| `receipt_identity_changed` | `cohort_operator` | Keep stage, organization and engagement fixed across revisions. |
| `receipt_predecessor_missing` | `cohort_operator` | Supply the missing earlier revision. |
| `receipt_not_admitted` | `intake_owner` | Admit the engagement in the register or correct the reference. |
| `correction_target_unknown` | `cohort_operator` | Name an existing current or earlier revision. |
| `correction_target_foreign` | `cohort_operator` | File the correction for the target's own organization. |
| `correction_target_not_creditable` | `cohort_operator` | Correct a stage receipt, not another correction. |
| `decision_readout_unknown` | `workflow_owner` | Bind the decision to a credited readout revision. |
| `decision_precedes_readout` | `workflow_owner` | Record a decision made at or after the credited real-case evaluation. |
| `timing_inconsistent` | `cohort_operator` | Check readiness against qualification and readout times. |
| `active_lease_needs_reconciliation` | `cohort_operator` | Reconcile effects and close the expired or stale lease. |
| `register_multiple_open_leases` | `custodian` | Restore the register; more than one open lease breaks its constraint. |

## Weekly view and reconciliation

The private report lists, for each column, the organizations and evidence references behind its
count, every correction, refused receipt and mismatch, and the next missing stage for each
organization with its owner and operator link. `verify_report` recomputes every count from those
lists and checks the headline, tier and outcome splits.

The weekly view (`opentour.pilot.cohort-funnel-weekly-view.v1`) is projected from a report that
passes `verify_report`. It holds counts, closed codes, durations, aggregate costs and the report
revision only: no organization, receipt, record, witness or owner reference and no per-workflow
age.

## Owner-run refresh

Keep the register, receipt batch, optional outcome receipts and the output directory private
(mode 0600 files in mode 0700 directories). From the source environment run:

```sh
python -m opentour_cell.pilot.cohort_funnel \
  --register /private/cohort/qualification.sqlite3 \
  --receipts /private/cohort/stage-receipts.json \
  --as-of 1795800000 \
  --setup-kind real_customer \
  --output /private/cohort/reports/funnel-1795800000.json
```

Add `--previous-report` with the last published report to chain revisions. Add
`--outcome-disclosure-permitted` with `--readiness`, `--tuning-benefit` and `--label-efficiency`
only when the owner permits outcome disclosure. The command opens the register read-only, writes
the private report once (it never overwrites a file) and prints only the weekly view. A future
`--as-of`, an unreadable register or an invalid batch exits 1 and publishes nothing. A missing
register is unavailable, not an empty cohort. An owner-run refresh uses zero Zap tasks; a future
scheduled Zap refresh must report its measured task usage from the Zap's own history.

## Rollback

The funnel is a read-only projection. To roll back, republish the previous report file: its
`report_revision` is unchanged and the register and receipts are untouched. A newer report names
the revision it supersedes in `supersedes_report_revision`. Rebuilding from the same register,
receipts, `as_of` and field map version reproduces the same revision. To correct a count, add a
correction or a superseding receipt revision; never edit a published report or a receipt.

## External gates

These stay open and are listed in every report's `unsatisfied_external_gates`:

* the CRM board, stage mapping and disclosure grant, and the scheduled reporting Zap with its
  task history;
* real customer stage receipts. The funnel has only generated fixtures, and the customer
  acceptance count stays 0/10;
* OPE-522's receipt adapter. Its readout reducer is unmerged; when it lands, its producer must
  emit the readiness receipt above or this consumer must change with it;
* OPE-517 results on real held-out workplace cases;
* OPE-566 signing and custody, without which no receipt can exceed `locally_keyed`;
* independent review of the exact commit.

From the pinned environment run
`just test-python services/cell-runtime/tests/pilot/test_cohort_funnel.py`.
