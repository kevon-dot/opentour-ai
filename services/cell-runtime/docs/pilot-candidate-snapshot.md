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
bytes. It is not keyed or signed; OPE-566 owns custody. Label digests are the one exception:
they are HMAC-SHA256 under a 32-byte key the producer creates on first use in its private
folder (`private/label.key`, mode 0600, in an owner-checked 0700 folder). That evidence level is
`locally_keyed`: it stops anyone from recovering a label by enumerating a small vocabulary,
but nobody outside the worker can verify it, and it is not a signature. If the key is missing
once snapshots exist, the producer refuses (`label_key_missing`) rather than re-keying. The
committed fixture (`tests/pilot/fixtures/candidate-snapshot-v1.json`) is generated. Nothing here
is provider, installed-Zap, Slack or customer evidence.

## Settings batch

`opentour.pilot.learning-profile-settings.v1`. Revisions start at 1, are consecutive, and are
write-once: identical settings replay, different settings conflict.

Who may commit. The `data_custodian` role is asserted by the resolved connection, bound on
first use; it is not authenticated until OPE-566. The first commit for a tenant and workflow
binds that actor ID as the only custodian (trust on first use). After that only a bound
custodian actor ID may commit (`custodian_not_bound`). Binding or unbinding a custodian takes
a currently bound custodian, and the last custodian cannot be unbound (`last_custodian`). Each
revision file records the committing actor ID and the binding revision it relied on, and the
binding chain is re-verified on every read. Any other or unknown role is refused. A connection
that asserts the role under an already bound actor ID is still believed.

Store identity. The first commit writes a random store ID inside the store. The store's
identity digests that ID with the tenant, workflow, and the real paths and device/inode
numbers of the store folder and its identity files. The snapshot producer pins this identity
into its snapshots folder on first use, and into every snapshot and receipt; the dependency
recorder pins it too. A copied, restored or second settings store is then refused with
`settings_store_mismatch` by `trigger`, `snapshot`, `current`, `record` and `training_parents`,
and a snapshot whose settings revision or digest the pinned store does not hold is refused the
same way.

The batch pins the tenant, workflow, intake-created benchmark and version, policy and
evaluation-policy revisions, engagement and grant epochs, the bound sources (role `decision`
or `correction`, and an evidence origin), the redacted field mapping, the decision-time
feature allowlist, the adjudicators (role `verifier` only), the abstain values, the freshness
and maturity windows, the development/protected split, the permitted uses, the receipt
counters the mandate allows, `min_reportable_cell` (optional, default 5, never below 2) and
`row_state_values` (optional, default admitted `["admitted"]`, disputed `["disputed"]`).

Redacted field mapping. Logical join field to source column; the committed fixture shows one.

| Side | Logical fields |
| -- | -- |
| decision | decision_id, decision_revision, case, entity, group, decision_time, prediction, features_available_at, features{name: column}, row_state |
| correction | correction_id, decision_id, decision_revision, case, correction_version, corrected_value, status, provenance, verifier, correction_time, row_state |

An unknown logical field is refused, and so is a mapping without `row_state`. The mapped
`row_state` column's values must fall in the closed `row_state_values` sets, whose admitted
and disputed values are disjoint; any other value is quarantined as `unknown_row_state`.
Columns that are not mapped are never read, and capture digests cover mapped columns only, so
a protected column that is present in the source is never consumed or disclosed, and changing
it changes neither a snapshot digest nor a receipt.

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
| `settings_store` | The pinned settings-store identity digest the snapshot was made against; null when held. |
| `policy_revision` | The policy revision the snapshot was built under; null when held. |
| `counters` | Counters restricted to the `receipt_counters` the settings allow. Each is an integer, or the string `"<k"` for a non-zero count below `min_reportable_cell`. |
| `reason_codes` | Reason code to count, only for codes that break down an allowed counter (see the table below), with the same `"<k"` rule. A held receipt carries its single hold code with count 1; that is the hold state, not a data cell. |

The same trigger, or the same canonical intent (tenant, workflow, settings digest and capture
fingerprints) under a different trigger, returns the same receipt. The same trigger reused for
a different read returns `held` with `trigger_conflict`.

## Receipt counters

| Counter | Meaning |
| -- | -- |
| `eligible` | Distinct cases with a verified observed development example whose grants permit training. |
| `pending` | Distinct observed cases without current verified truth. |
| `quarantined` | Malformed or unattributable rows; they count zero everywhere else. |
| `disputed` | Distinct observed cases excluded as disputed. |

Live totals count distinct cases, not decisions. A case with several decisions is disputed if
any of them is, otherwise pending if any is, and verified only when all are. The recurrence
ratio (`wrong_automatic_decisions`) counts distinct cases in both numerator and denominator.
Starter evidence is counted separately inside the snapshot and never enters these counters,
recurrence or training parents. A zero denominator is reported as `unavailable`.

Small cells. A non-zero count below `min_reportable_cell` is disclosed as `"<k"`, in counters
and reason codes alike. Suppression is per cell: differencing two receipts whose cells are at
or above the threshold can still reveal a change of that size. The OPE-526 exposure guard owns
differencing control across released cells; this receipt does not claim it.

## Reason codes

| Code | Kind | Breaks down |
| -- | -- | -- |
| `malformed_decision` | quarantine | quarantined |
| `malformed_correction` | quarantine | quarantined |
| `unknown_row_state` | quarantine | quarantined |
| `unknown_correction_status` | quarantine | quarantined |
| `unknown_provenance` | quarantine | quarantined |
| `future_decision_features` | quarantine | quarantined |
| `ambiguous_join` | quarantine | quarantined |
| `redundant_delivery` | exclusion | quarantined |
| `duplicate_correction` | exclusion | quarantined |
| `orphan_correction` | exclusion (outcome waiting for a decision) | quarantined |
| `stale_revision` | exclusion | quarantined |
| `source_disputed` | disputed | disputed |
| `contradictory_decision` | disputed | disputed |
| `decision_case_changed` | disputed | disputed |
| `correction_id_conflict` | disputed | disputed |
| `contradictory_corrections` | disputed | disputed |
| `missing_decision_predecessor` | pending | pending |
| `awaiting_outcome` | pending | pending |
| `superseded_decision_revision` | pending | pending |
| `missing_correction_predecessor` | pending | pending |
| `late_correction_revision` | pending (held for reconciliation) | pending |
| `outcome_before_decision` | pending (held for reconciliation) | pending |
| `reopened` | pending | pending |
| `correction_withdrawn` | pending | pending |
| `non_verifying_provenance` | pending | pending |
| `unattested_label` | pending | pending |
| `correction_immature` | pending | pending |
| `starter_origin` | verified, not a learning candidate | eligible |
| `protected_partition` | verified, evaluation only | eligible |
| `training_not_permitted` | verified, no training grant | eligible |
| `group_crosses_split` | verified, excluded by the partitioner | eligible |
| `development_label_after_split` | verified, excluded by the partitioner | eligible |
| `decision_absent` | current-eligibility retraction | eligible |
| `settings_missing` | hold | none (hold) |
| `source_unbound` | hold | none (hold) |
| `foreign_tenant_capture` | hold | none (hold) |
| `capture_after_as_of` | hold | none (hold) |
| `source_stale` | hold | none (hold) |
| `policy_changed_during_read` | rollback | none (hold) |
| `no_committed_snapshot` | hold | none (hold) |
| `trigger_conflict` | hold | none (hold) |
| `snapshot_commit_conflict` | hold | none (hold) |
| `settings_store_mismatch` | hold | none (hold) |
| `label_key_missing` | hold | none (hold) |

## Interpretations recorded as gaps

- **Late revision.** A correction revision that changes a final whose maturity window had
  already closed, without an intervening reopen, is held as `late_correction_revision`. A
  reopen is the explicit path to change matured truth.
- **Maturity.** A correction is not verified until its maturity window after the correction
  time has closed (`correction_immature`).
- **OPE-516 split name.** OPE-516 calls its seed population `training`; this producer's
  populations are `development` and `protected`. The `to_seed_roots` projection maps a
  development training parent to `training`.
- **OPE-544 references.** OPE-544's manifest has no field that marks a value caller-asserted,
  so `to_lineage_manifest` refuses any caller-supplied reference (`lineage_ref_caller_asserted`)
  and projects only references the worker mints from its own records (`lineage_refs`): the
  committed snapshot as the result, the example as the candidate, and one claim per record.
  Caller-asserted artifacts and relations are not projected. No worker-side producer of
  OPE-544 workflow claims or frozen results exists yet.
- **Proposal references.** Proposal references come from consumers. They are labelled
  `caller_asserted` wherever they surface (`stale_proposals` in current eligibility and in the
  training-parent manifest).
- **Synthetic union.** `union_synthetic` verifies the parents manifest it is handed against
  the copy this worker stored under that digest before copying any count.
- **Seal scope.** The seal covers `dataset_preparation` records only, so an evaluation or
  other record made after sealing never conflicts with it. Dataset records and sealing share
  one lock.
- **Holds are not memoized.** A held trigger commits nothing. Retrying the same trigger later
  re-evaluates it: a trigger held at 09:00 for a stale source can commit at 11:00.
- **Settings window.** The producer re-reads the settings after reading the sources and again,
  under the settings store's commit lock, immediately before writing the snapshot. A commit
  between the read and the write rolls back; one after the write applies to the next
  snapshot. This assumes every writer goes through `SettingsBatchStore`.
- **Mandate disclosure.** The standing mandate does not define which counters a receipt may
  disclose, so the settings batch carries `receipt_counters`.
