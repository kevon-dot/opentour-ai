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
| OPE-430 admission against actual store | Same live admission vectors with durable resolver; superseded no source read/admission; revoked retry no earlier run ref | Pending composed intake authority and SDK proof |
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
