# Verified-candidate snapshot producer (OPE-416, worker side)

This is the worker-side half of OPE-416. It binds a versioned per-pilot settings batch, joins
the source pair on the customer's worker, commits an immutable as-of snapshot and returns an
opaque receipt. The private Zapier action, its Zap template and any Slack surface are a
separate lane. They consume the receipt described below and nothing else.

Modules:

- `opentour_cell.pilot.learning_profile`: the settings batch, its write-once store and the
  source-pair mapping.
- `opentour_cell.pilot.candidate_snapshot`: the reducer, the snapshot producer, the receipt and
  the current-eligibility reducer.
- `opentour_cell.pilot.consumed_dependencies`: the consumed-dependency manifest (written at
  actual consumption), its seal, the training-parent manifest and the projections that
  OPE-544, OPE-525 and OPE-516 read.

Evidence tier: every record is `digest_self_consistent_only`. A digest agrees with its own
bytes. It is not keyed or signed; OPE-566 owns custody. The committed fixture
(`tests/pilot/fixtures/candidate-snapshot-v1.json`) is generated. Nothing here is provider,
installed-Zap, Slack or customer evidence.

## Settings batch

`opentour.pilot.learning-profile-settings.v1`. Only a principal bound as `data_custodian` may
commit it; any other or unknown role is refused. Revisions start at 1, are consecutive, and
are write-once: identical bytes replay, different bytes conflict.

The batch pins the tenant, workflow, intake-created benchmark and version, policy and
evaluation-policy revisions, engagement and grant epochs, the bound sources (role `decision`
or `correction`, and an evidence origin), the redacted field mapping, the decision-time
feature allowlist, the adjudicators (role `verifier` only), the abstain values, the freshness
and maturity windows, the development/protected split, the permitted uses and the receipt
counters the mandate allows.

Redacted field mapping. Logical join field to source column; the committed fixture shows one.

| Side | Logical fields |
| -- | -- |
| decision | decision_id, decision_revision, case, entity, group, decision_time, prediction, features_available_at, features{name: column}, optional row_state |
| correction | correction_id, decision_id, decision_revision, case, correction_version, corrected_value, status, provenance, verifier, correction_time, optional row_state |

An unknown logical field is refused. Columns that are not mapped are never read, so a
protected column that is present in the source is never consumed or disclosed.

## Receipt fields

`opentour.pilot.candidate-snapshot-receipt.v1`. These are the only fields. No tenant, case,
decision, group, feature, prediction or label value can appear.

| Field | Meaning |
| -- | -- |
| `schema` | The receipt schema string. |
| `state` | `committed`, `rolled_back` (policy changed mid-read; this is the previous committed snapshot) or `held`. |
| `reason` | A hold or rollback reason code, otherwise null. |
| `snapshot_digest` | SHA-256 of the committed snapshot; null when held. |
| `digest_tier` | Always `digest_self_consistent_only` today. |
| `policy_revision` | The policy revision the snapshot was built under; null when held. |
| `counters` | Integer counters, restricted to the `receipt_counters` the settings allow. |
| `reason_codes` | Reason code to integer count, drawn from the closed vocabulary below. |

The same trigger, or the same canonical intent (tenant, workflow, settings digest and capture
fingerprints) under a different trigger, returns the same receipt. The same trigger reused for
a different read returns `held` with `trigger_conflict`.

## Receipt counters

| Counter | Meaning |
| -- | -- |
| `eligible` | Verified observed development examples whose grants permit training. |
| `pending` | Distinct observed decisions without current verified truth. |
| `quarantined` | Malformed or unattributable rows; they count zero everywhere else. |
| `disputed` | Distinct observed decisions excluded as disputed. |

Starter evidence is counted separately inside the snapshot and never enters these counters,
recurrence or training parents. A zero denominator is reported as `unavailable`.

## Reason codes

| Code | Kind |
| -- | -- |
| `malformed_decision` | quarantine |
| `malformed_correction` | quarantine |
| `unknown_row_state` | quarantine |
| `unknown_correction_status` | quarantine |
| `unknown_provenance` | quarantine |
| `future_decision_features` | quarantine |
| `ambiguous_join` | quarantine |
| `redundant_delivery` | exclusion |
| `duplicate_correction` | exclusion |
| `orphan_correction` | exclusion (outcome waiting for a decision) |
| `stale_revision` | exclusion |
| `source_disputed` | disputed |
| `contradictory_decision` | disputed |
| `decision_case_changed` | disputed |
| `correction_id_conflict` | disputed |
| `contradictory_corrections` | disputed |
| `missing_decision_predecessor` | pending |
| `awaiting_outcome` | pending |
| `superseded_decision_revision` | pending |
| `missing_correction_predecessor` | pending |
| `late_correction_revision` | pending (held for reconciliation) |
| `outcome_before_decision` | pending (held for reconciliation) |
| `reopened` | pending |
| `correction_withdrawn` | pending |
| `non_verifying_provenance` | pending |
| `unattested_label` | pending |
| `correction_immature` | pending |
| `starter_origin` | verified, not a learning candidate |
| `protected_partition` | verified, evaluation only |
| `training_not_permitted` | verified, no training grant |
| `group_crosses_split` | verified, excluded by the partitioner |
| `development_label_after_split` | verified, excluded by the partitioner |
| `decision_absent` | current-eligibility retraction |
| `settings_missing` | hold |
| `source_unbound` | hold |
| `foreign_tenant_capture` | hold |
| `capture_after_as_of` | hold |
| `source_stale` | hold |
| `policy_changed_during_read` | rollback |
| `no_committed_snapshot` | hold |
| `trigger_conflict` | hold |
| `snapshot_commit_conflict` | hold |

## Interpretations recorded as gaps

- **Late revision.** A correction revision that changes a final whose maturity window had
  already closed, without an intervening reopen, is held as `late_correction_revision`. A
  reopen is the explicit path to change matured truth.
- **Maturity.** A correction is not verified until its maturity window after the correction
  time has closed (`correction_immature`).
- **OPE-516 split name.** OPE-516 calls its seed population `training`; this producer's
  populations are `development` and `protected`. The `to_seed_roots` projection maps a
  development training parent to `training`.
- **OPE-544 references.** The OPE-544 reader needs result, candidate and claim references
  that this writer does not produce. `to_lineage_manifest` takes them as caller-asserted
  inputs once they exist.
- **Mandate disclosure.** The standing mandate does not define which counters a receipt may
  disclose, so the settings batch carries `receipt_counters`.
