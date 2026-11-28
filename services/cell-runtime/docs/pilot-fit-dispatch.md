# Reserve, dispatch and settle one authorized fit (OPE-418)

`opentour_cell.pilot.fit_dispatch` turns one fit intent into at most one reservation, one backend job and one settlement. It holds across duplicate triggers, lost acknowledgements, worker restarts and concurrent workers. It runs on the customer's worker. It never authorizes a handoff, publication or export.

**Evidence tier:** `fixture`. The tests use generated sentinels, a durable test double of the ledger port and a simulated backend. No real weights, provider account, billing or spend are involved.

## Boundaries the dispatcher does not own

| Boundary | Owner | What the dispatcher relies on |
| --- | --- | --- |
| `FitLedger` port | OPE-442 | Each method is one durable transaction. `reserve` aliases a client-scoped operation key to the canonical intent. Inside its own writer transaction it calls the dispatcher's recheck, reserves the upper bound and emits one execution ID. `transition` is a compare-and-set on a version number. `settle` runs once. The tests use `tests/pilot/fit_dispatch_doubles.py`, which is **not** the OPE-442 ledger. With no ledger configured, every dispatch holds as `fit_ledger_unavailable`: nothing is reserved and nothing is sent. |
| `model_connector.Preflight` | OPE-506 | All discovery, estimation, submission (`submit_reserved_fit`, under the dispatcher's guard), reconciliation and candidate collection go through it. Status, cancel and usage call the connector directly. Progress, early stop, absence confirmation and hold reasons are optional protocols. |
| `RepairRoutingStore.current_eligibility` | OPE-417 | Called inside the reservation recheck, inside the submission guard just before the backend call, and on every supervision tick. A stored `fit_eligible` receipt is never authority on its own (PR #205 contract). |
| `LearningMandates.dispatch_authority` | this lane | A read-only view of the current standing mandate, taken under the engagement guard. It returns the caps, the payer, the profile and the run binding. It also returns the renewal lineage budget scope and the direct-path usage, which the dispatcher adds to the ledger's own usage. |

## Stages

`reserved` → `submitting` → `running` → `stopping` → `settled`.

Two stages are durable holds:

* `submission_uncertain`: the backend has neither confirmed the job nor confirmed that no job exists.
* `held`: usage is over a reserved bound or cannot be verified.

Ordering rules:

* Every backend effect follows a compare-and-set that only one worker can win.
* `submitting` carries a lease token that is unique to each dispatcher instance. Any other instance reconciles only after the lease expires. This includes a worker that restarts under the same identity, because a restart cannot be told apart from a slow submission. A reconciliation is always a read-only lookup, never a resend.
* A submission is resent only after the backend affirmatively confirms that no job exists. At most two attempts are made, under the same reservation and the same provider idempotency key (the execution ID).
* A backend that cannot confirm absence (no OPE-506 connector can today) leaves the reservation held for a custodian.
* `status` is read-only. It never calls the backend and never writes the ledger.
* Withdrawal, changed evidence and a revoked training grant are detected on the next `advance`. While a fit is live, the worker should call `advance` on a short cadence.
* The ledger's recheck runs inside its writer transaction and reads the mandate store, the routing store and the grant source. An OPE-442 ledger must not hold a lock those reads need.

## Policy field mapping (the reservation record)

| Persisted field | Source |
| --- | --- |
| tenant, engagement, actor (delegate) | Request. Checked against the binding, the receipt and the mandate. |
| mandate ID and revision, payer, owner | `dispatch_authority` |
| budget scope and period | Renewal lineage of the standing mandate. Caps are cumulative across renewals, with no periodic reset (`mandate_lineage`). |
| snapshot, evaluation ID and revision, run | The residual receipt the intent cites. |
| exact model binding (model, revision, task, schemas, route, machine or account, region, credential reference), model digest | Request. The model must match the receipt's frozen selection and the mandate profile. |
| benchmark revision, grant revision, development digest, fields | Request. Checked by `Preflight` against the live training grant. |
| license | Request (`license_ref`, from the qualified model family). |
| bound: currency, maximum minor units, seconds, rows, steps | Request. Must sit within the mandate caps and the grant's fit bound, and must cover the estimate. |
| limits: money cap, fit count, total seconds, seconds per fit, rows per fit, maximum concurrent fits | Mandate caps (`max_cost_cents` in USD cents, `max_fits`, `max_total_seconds`, `max_seconds_per_fit`, `max_rows_per_run`). Concurrency defaults to 1. |
| compute | `bound_seconds_on_named_route`. The OPE-506 `FitBound` has no separate compute dimension, so the compute ceiling is the bound's seconds on the bound machine or account. |
| recipe and its revision | Identity, split, metric, direction, step limit, stopping rule and fit count must match the receipt's selection. Cadence, minimum improvement, patience and the fixed-length fallback are pinned here. |

**Dedupe key:** `intent_key = digest(tenant, mandate revision, snapshot, plan revision, model digest)`. The execution ID is derived from it.

* The same full intent under another operation key returns the same execution.
* A reused key carrying a changed intent is refused as `fit_operation_key_conflict`.
* A new key with the same dedupe key but a changed intent is refused as `fit_intent_conflict`.

## Money, limits and holds

**Charging.** A reservation is charged its full upper bound whatever it later uses. This matches the standing mandate and OPE-578.

**Limits.**

* A paid bound under a standing mandate whose money cap is 0 holds as `mandate_cap_required`. That is every mandate today, until OPE-426 records a paid cap.
* Limits are enforced locally while the fit runs. Reported steps or seconds above the bound, or live usage above any bound, cancel the job.

**Settling and holding a terminal job.** A terminal job settles only when its usage is verifiably within every reserved dimension. Otherwise it is held as `over_cap`, `usage_unverifiable` or `cap_unverifiable`, which is the OPE-508 vocabulary. A connector's own `hold_reason` wins when it has one. A held job:

* keeps its reservation charged, raised to the real figure when that is higher and never clamped;
* records that real figure;
* is never collected, so its tuned-model ID or artifact is never used;
* never settles.

Releasing a held reservation needs the operator decision that OPE-570 will define. Until then the ledger refuses every transition out of `held`.

The OPE-507 open-weight connector reports no usage for a canceled job. A revoked local fit is therefore held as `usage_unverifiable`, not settled.

## Stopping

Stopping follows the immutable recipe:

* A nonfinite metric, a progress report off the recipe's cadence, invalid output, revoked authority, changed evidence and owner cancellation all stop the fit with no candidate. The last valid checkpoint is recorded as a sealed partial reference that is never publishable.
* A plateau (`patience` evaluations without at least `min_improvement` in the recipe's direction) is a completion. The best validation checkpoint is restored only when the backend declares checkpoint recovery. Otherwise the checkpoint at the stop is used, and the selection method records which happened.
* A backend without progress and early-stop controls runs the recipe's preapproved fixed-length fallback. Without that fallback it holds as `early_stop_unsupported`.
* The backend answers an early-stop request with an `EarlyStopAck` that says whether the request ended the job and which checkpoint it restored. The record follows that answer.
* A backend that had already finished is recorded as an ordinary completion at its final step.
* An early-stop callback that fails, or whose answer cannot be matched to the job, becomes a cancellation with no candidate.

## Acceptance map

All tests are in `tests/pilot/test_fit_dispatch.py` and use generated fixtures.

| Acceptance (OPE-418) | Test |
| --- | --- |
| Two concurrent eligible dispatches with budget for one: exactly one fit, the other holds with a reason | `test_two_concurrent_dispatches_with_budget_for_one_admit_exactly_one`, `test_concurrency_limit_holds_the_second_fit_until_the_first_settles` |
| Same intent under two keys with budget for two, first response lost, restart: one reservation, execution ID, fit and settlement | `test_same_intent_under_two_keys_lost_response_and_restart_is_one_fit` |
| Changed intent on a reused key conflicts | same test, plus `test_operation_keys_are_scoped_to_the_authenticated_client` |
| Disconnect after worker acceptance, restart: exactly one fit and one settlement | `test_worker_crash_after_acceptance_restart_is_exactly_one_fit`, `test_a_restarted_worker_honours_its_own_unexpired_lease`, `test_crash_before_submission_and_before_settlement_recover_once` |
| Revoke during fit: no later publication or export | `test_revocation_during_fit_cancels_and_never_publishes_or_exports`, `test_invalidated_evidence_or_revoked_grant_stops_an_in_flight_fit`, `test_revocation_between_reservation_and_submission_sends_nothing` |
| Rule suffices: zero training starts | `test_a_rule_that_suffices_starts_zero_fits` |
| `current_eligibility` before reserving and before starting | `test_eligibility_is_recomputed_inside_the_reservation_and_before_submission`, `test_a_fit_eligible_receipt_alone_never_reserves` |
| Fail closed without the OPE-442 ledger | `test_without_a_ledger_nothing_is_reserved_or_sent`, `test_a_ledger_failure_never_submits`, `test_an_unavailable_collaborator_holds_with_nothing_reserved` |
| Matching model, capability, benchmark, recipe, split, residual and mandate revisions | `test_recipe_model_capability_and_receipt_revisions_must_match`, `test_a_model_outside_the_mandate_profile_is_refused` |
| Reserve the maximum authorized cost before the first call; bind the record | `test_reservation_record_binds_model_benchmark_backend_and_limits` |
| Unknown cost or unsupported hardware is a setup hold | `test_unknown_cost_or_unsupported_hardware_is_a_setup_hold` |
| No training capability: benchmark only, zero fits | `test_backend_without_training_is_benchmark_only_with_zero_fits` |
| Only the authorized development payload reaches a managed backend | `test_a_managed_route_receives_only_the_authorized_development_payload` |
| Plateau, improving run, unsupported early-stop callback | `test_plateau_stops_early_and_restores_the_best_validation_checkpoint`, `test_plateau_without_checkpoint_recovery_records_the_stop_checkpoint`, `test_an_improving_run_completes_at_the_recipe_step_limit`, `test_an_early_stop_the_backend_did_not_apply_is_recorded_as_a_completion`, `test_early_stop_recipe_holds_on_a_backend_without_early_stop`, `test_unsupported_early_stop_runs_a_preapproved_fixed_length_recipe`, `test_a_failing_early_stop_callback_stops_with_no_candidate`, `test_stopping_decision_is_pure_and_follows_the_recipe` |
| Stop on a nonfinite metric, invalid output or a limit | `test_nonfinite_invalid_or_over_limit_progress_stops_with_no_candidate`, `test_invalid_output_is_never_published`, `test_live_usage_above_the_bound_cancels_and_holds` |
| Uncertain submission held until the backend confirms its job or confirms no job | `test_lost_ack_with_no_visible_job_holds_until_the_backend_confirms`, `test_uncertain_submission_without_absence_confirmation_stays_held`, `test_confirmed_absence_resubmits_once_under_the_same_reservation` |
| One-fit mandate: one logical job and settlement across retries | `test_a_one_fit_mandate_is_one_logical_job_and_settlement_across_retries` |
| Over-cap and unverifiable usage held, tuned-model ID never released (OPE-508, OPE-570) | `test_over_cap_or_unverifiable_usage_is_held_and_never_collected`, `test_paid_over_cap_is_charged_at_the_real_figure_and_blocks_the_next_fit`, `test_a_connector_hold_reason_wins_over_usage_that_looks_within_bounds`, `test_usage_hold_matches_the_ope508_vocabulary` |
| Cancellation; status reads have no effects | `test_cancel_before_submission_releases_and_cancel_while_uncertain_is_pending`, `test_development_payload_retry_is_bounded`, `test_status_reads_have_no_effects` |
| `dispatch_authority` is read-only, refuses lapsed authority and keeps one budget across renewals | `test_dispatch_authority_reports_one_lineage_budget_and_refuses_lapsed_authority` |
| The route connectors still expose what the dispatcher reads (drift pin) | `test_the_route_connectors_expose_what_the_dispatcher_consumes` |
| Training completion confers no handoff authority | `handoff_authority == "none"` and `publication == "independent_review_required"` on every status. Asserted in the plateau and revocation tests. |

## External gates (not satisfied here)

* The OPE-442 durable customer-local fit ledger. It must implement `FitLedger`; production has none, so dispatch holds.
* A real pinned-weights fit in an approved sandbox, with a nonzero adapter change and replay in a fresh process. This also needs OPE-377 for a customer's live model arm.
* Real provider spend, billing and charge reconciliation.
* The OPE-570 operator release of held over-cap reservations.
* OPE-566 signed reservation and settlement receipts. Today's records are `digest_self_consistent_only` at most.
* OPE-426 owner settings for a paid cap and a concurrency setting.
* The learning-run endpoints (OPE-559) that resolve a plan into a `DispatchRequest`.
* Packaging `fit_dispatch` into the Slack pilot kit. It is not listed in `tools/build_slack_pilot_kit.py`.

## Run the tests

```sh
just test-python services/cell-runtime/tests/pilot/test_fit_dispatch.py
```
