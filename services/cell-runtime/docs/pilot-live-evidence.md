# Live evidence implementation record

Issue: OPE-514. Governing native specification and implementation contract were
read on 2026-11-26. Source base: #106 `4953827ba2855f6b9e4a29f1471448ef4be3ba71`.
This is a worker implementation in progress, not a completed issue or installed
provider witness. Generated fixtures contain opaque references only.

| Acceptance family | Positive and adverse oracle | Planned or actual selector |
| --- | --- | --- |
| Business identity versus delivery identity | D1 delivered three times, D2 same case/family, O1 only targets D1; restart 2/1/1, one family; redundant delivery adds zero | `test_redelivered_decision_counts_once_and_distinct_decisions_count_twice` |
| Conflicting payload at one decision revision | One disputed D1; D2 remains pending; prior as-of view retained | `test_same_decision_same_revision_two_event_ids_disputed`, `test_conflicting_delivery_revision_is_disputed` |
| Exact decision revision join | D1 revision 2 removes verification at revision 1; no second decision; later exact truth restores it | `test_new_decision_revision_reopens_verification` |
| Outcome revisions | Final v1, reopen v2, stale v1, final v3 produce 1/0, 0/1, 0/1, 1/0; original inputs unchanged | `test_final_reopen_stale_final_reverify_preserves_original_input` |
| Late and unauthorized truth | Outcome before commit buffered; wrong verifier quarantined, not counted | `test_outcome_before_decision_is_buffered_and_wrong_verifier_quarantined` |
| Missing original input, content off | Only committed-action agreement; no input or raw-model claim; extra raw content rejected without storage | `test_import_without_input_is_metric_only_and_content_off` |
| Binding durability and current authority | Save twice same opaque ref, successor hold, revoked/foreign/old epoch unavailable | `test_binding_saved_once_successor_revocation_and_old_epoch` |
| Epoch checkpoint | Exclude old checkpoint rows and late old-epoch truth at a newer source position | `test_new_epoch_binding_excludes_prior_rows_and_late_old_epoch_outcome` |
| Explicit custodian role; zero-effect reads | Owner without binding role cannot save; reading never initializes DB or changes it | `test_binding_requires_custodian_and_read_has_zero_writes` |
| Page idempotency | Same source page returns same receipt; contradictory same page held | `test_page_retry_same_receipt_and_conflicting_page_holds` |
| Verifier enrollment versus binding | A permitted grant alone does not mean a binding was saved | `test_verifier_grant_is_not_a_saved_binding` |
| Current eligibility after revocation | Removed outcome resource contributes zero verified credit; revision changes | `test_revoked_truth_cannot_continue_to_credit_current_verification` |
| Real emitted settings action | Connect live decisions saves current custodian-signed binding twice across restart; unchanged benchmark | Pending UI composition and actual worker authority configuration |
| OPE-430 admission against actual store | Same live admission vectors with durable resolver; superseded no source read/admission; revoked retry no earlier run ref | `test_actual_binding_store_vectors_and_restart`, `test_normal_intake_factory_uses_explicit_live_selection`, separate `test_live_authority.py` official-SDK executable witness |
| Authorized reader, atomic cursor and restart | Real scoped source adapter normalization and page lineage; paused feed and two tenants with colliding IDs | Pending reader composition; OPE-434 owns reader extension |
| Downstream invalidation | Current evidence revision fences stale readiness; existing snapshots/proposals invalidated before use | Pending OPE-542 integration and future snapshot/proposal consumers |

`LiveAuthority.guard` is an internal worker port, not a wire payload or a grant
minting API. It must pin actual current custody, signed resource grants and actor
rights for the complete operation. The reducer accepts events only through a
binding saved under that authority. `snapshot` and `resolve` open existing SQLite
state read-only; neither polls a source, starts a fit, nor changes a benchmark.

The current implementation does not yet claim a live coverage window, a complete
empty-source read, original raw model input/output, fit timing, or delivery.
Those facts require their respective independently sourced producer receipts.

The normal `opentour_cell.pilot.intake_local` stdio executable accepts a private
`opentour.pilot.local-live-selection.v1` selection with exactly `schema_version`,
`tenant`, `workflow_ref`, and `actor`. It consumes the existing signed authority;
the selection itself grants no rights. The current adapter reports
`live_reader_unavailable` until a real source reader is configured.

The separate SDK witness runs `test_live_authority.py` directly in a pinned
CPython 3.12.16 / MCP SDK 1.30.0 environment, with the source package and pilot
test helpers on `PYTHONPATH`. It uses two actual stdio subprocess sessions,
three equivalent submissions, an empty catalog before binding, restart,
superseded-binding hold and revoked retry. All records are generated. A passing
receipt confirms zero source reads and zero live events; this admission witness
does not certify ingestion, an installed customer, or a complete issue.

Current evidence requires a source origin with the complete current grant,
including verifier, namespace, lease and checkpoint. Rotating a grant does not
reauthorize old truth. An actual permitted reread may add a new origin to an
identical delivery without counting it twice; the namespace comes from that
origin. Conflicting case/family attributes contribute only independently
unambiguous grouping identities, so arrival order does not change totals.
The independent verifier-rotation, grouping-order and namespace-reread probes
are retained as regression tests in `test_live_evidence.py`.

An authorized reader may attach exactly `interval_start`, `interval_end`,
`coverage_through`, `complete`, and `next_cursor` to a page. The reducer stores
these with its checkpoint, accepted page receipt, supplied row count and actual
poll time in the same transaction. Unknown intervals and coverage stay null;
zero rows alone do not establish complete coverage. Failed pages create no read
receipt. `read_receipts` only reads current authorized page receipts. Snapshot
`unmatched_outcomes` counts distinct verifier/outcome identities for unresolved
exact decision targets, deduplicating redelivery and outcome revisions;
`last_ingested_at` describes the accepted event ingestion time, not source
freshness or complete interval coverage. The actual adapter and UI exposure of
these facts are separate integration work.

The private Slack workflow selection accepts the same bounded reference as intake,
including historical workflow names. Connect live decisions checks the selected
workflow's admission, active engagement and current signed custodian grant on Save.
The selector alone gives the caller no access.

For a current workflow with aggregate inspection rights, Home reports unmatched
outcomes and when the worker last ingested an event. It also identifies the latest
acknowledged source page, its poll time, row count, queried interval, coverage
through time, and completion state. These values describe the single acknowledged
page. A zero-row poll can advance coverage without adding an event. An unavailable
interval or watermark stays unavailable.
