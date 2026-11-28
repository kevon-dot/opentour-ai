# Learning-run endpoints for Slack, Zapier and the schedule

Use these endpoints to request, track and cancel one bounded OpenTour learning run on the customer's own worker. The endpoints control runs only. They are separate from the EVE inference API and from the private trainer. A learning run never carries or implies release approval. Every release still needs its own exact-version approval.

The source of truth for every name in this guide is `opentour_cell/pilot/learning_runs.py`. A test checks the tables below against it.

## Who can call

Each connection is a separate permission. The owner authorizes it on the worker host. Nothing on the wire can create, widen or extend a grant.

* **Hosted Slack or Zapier adapter.** It calls the TLS gateway with its own bearer credential and audience. The grant binds that connection to one workspace (tenant), one actor, one workflow and one standing mandate.
* **Slack Socket Mode on the owner host.** It calls the same handlers in process through `learning_run_dispatch`. The handlers, admission and ledger are the same as for TLS.
* **The worker's schedule tick.** It uses its own scheduler grant on the worker volume. It is never served over TLS.

| Role | Permissions |
| -- | -- |
| `owner` | `start`, `read`, `cancel`, `usage` |
| `operator` | `read`, `usage` |
| `reviewer` | `read` |
| `scheduler` | `start`, `read`, `dispatch` |

Only the owner can start or cancel a run from Slack or Zapier. The `owner` actor must be the standing mandate's owner. The `scheduler` actor must be the mandate's delegate. Usage figures appear only for roles with `usage`.

## Routes

The TLS gateway builds its request lines from the same table. An intake or health listener does not serve these routes and returns 404.

| Method | Path | Operation |
| -- | -- | -- |
| `POST` | `/v1/learning-runs` | `submit_learning_run` |
| `GET` | `/v1/learning-runs/{run_ref}` | `get_learning_run` |
| `POST` | `/v1/learning-runs/{run_ref}/cancel` | `cancel_learning_run` |

Send `application/json` with Content-Length, the approved Host and Origin, `X-OpenTour-Audience` and the bearer credential, over verified TLS. A status read has no body. Limits match the intake gateway: 8 KiB headers, 4 KiB arguments, 32 KiB result and 120 calls per minute per listener.

### Start a run

```json
{"plan_ref": "plan-…", "operation": "evaluate_and_improve", "idempotency_key": "slack-action-…"}
```

The body has exactly these three fields. `operation` is `benchmark_only` or `evaluate_and_improve`. Any other field is refused with `invalid_arguments`, including a backend URL, command, examples, answer keys, secrets or a release action.

`plan_ref` names a plan the worker derived. Callers cannot write plans. The worker resolves the workflow, benchmark, baseline, incumbent, recipe, model route and standing mandate from that plan. A plan is current only while it matches the current mandate revision and the latest verified source revision. A stale plan is held with `plan_stale`; read the current plan and try again.

### Responses

An admitted run returns its status projection (below). The first admission and every replay return the same `run_ref`.

When nothing is admitted, the response has no `run_ref`:

```json
{"contract": "opentour.pilot.learning-runs.v1", "state": "held", "reason_code": "standing_mandate_paused",
 "next_action": "resume_mandate", "next_actor": "owner",
 "release_authority": "separate_exact_version_approval_required"}
```

### Read a run

`GET /v1/learning-runs/{run_ref}` returns only what the caller's role may see:

```json
{"contract": "opentour.pilot.learning-runs.v1", "run_ref": "lrun-…", "state": "running",
 "stage": "discovery", "operation": "evaluate_and_improve",
 "effective_operation": "evaluate_and_improve", "reason_code": null,
 "next_action": "wait_for_worker", "next_actor": "worker", "owner": "…",
 "plan_ref": "plan-…", "workflow_ref": "…", "progress": {"completed": 3, "total": 10},
 "evidence_refs": ["…"], "cancellation": null,
 "release_authority": "separate_exact_version_approval_required",
 "usage": {"fit_reservations": 1, "reserved_units": 90, "consumed_units": null}}
```

`progress` appears only when the worker measured it. `evidence_refs` are opaque references. `usage` appears only for `owner` and `operator`. A status read never starts, fences, settles or changes anything.

### Cancel a run

```json
{"idempotency_key": "slack-cancel-…"}
```

The response is the run's single cancellation receipt:

```json
{"contract": "opentour.pilot.learning-runs.v1", "run_ref": "lrun-…", "receipt_ref": "lcancel-…",
 "cancellation": "pending", "state": "cancellation_pending",
 "release_authority": "separate_exact_version_approval_required"}
```

* `confirmed`: the run is `canceled`. No backend work was running, or the worker confirmed that it stopped, and the ledger settled the consumed budget.
* `pending`: backend work or a reservation may still exist. The run shows `cancellation_pending` until the worker confirms the terminal state and the ledger settles. Nothing is refunded below the ledger's own charge.
* `not_applicable`: the run had already finished, was refused, or reached review first. Cancellation does not undo finished work. A candidate awaiting review still needs its own release decision.

Every cancel request for a run returns the same receipt. Reusing a cancel key for a different run conflicts.

## Errors

| Code | HTTP status |
| -- | -- |
| `invalid_arguments` | 400 |
| `learning_run_unavailable` | 403 |
| `learning_run_role_required` | 403 |
| `learning_run_expired` | 403 |
| `learning_run_capacity_reached` | 403 |
| `idempotency_conflict` | 409 |
| `learning_run_rate_limited` | 429 |

An unknown run, a run of another tenant or workflow, a withdrawn grant and a wrong selection all return `learning_run_unavailable`. A withdrawn grant reveals no earlier run reference.

## One intent, one run

The canonical intent is the tenant, the plan reference and the requested operation. The plan reference is the digest of the frozen plan, so it already binds the workflow, mandate revision, verified source revision, incumbent, benchmark, baseline, recipe and model route. The channel, actor, client and idempotency key are not part of the intent.

* The same intent from Slack, Zapier and the schedule returns one run, whichever arrives first. Each trigger is recorded against that run.
* A retry with the same key returns the same run. Retry a lost response with the original key, never a new one.
* Reusing a key for a different plan or operation returns `idempotency_conflict`.
* A run has at most one fit reservation. The worker reserves it through the OPE-442 ledger port, keyed by tenant, mandate revision, snapshot, plan and model digest.

## States

| State | Meaning |
| -- | -- |
| `queued` | Admitted with a possible fit. Waiting for the worker. |
| `benchmark_only` | Admitted with zero fits. Waiting for the worker. `reason_code` says why. |
| `running` | The worker holds the run. `stage` says what it is doing. |
| `verifying` | The worker is verifying a candidate. |
| `review_ready` | A candidate is ready for the named reviewer. This grants no release. |
| `held` | Blocked until the named actor acts, for example to reconcile an uncertain trainer submission. |
| `cancellation_pending` | Cancellation requested. The backend result or settlement is not yet confirmed. |
| `completed` | Finished. |
| `failed` | The worker could not finish. |
| `refused` | Never dispatched. Its mandate was paused, withdrawn, expired or replaced. |
| `canceled` | Cancellation confirmed and consumed budget settled. |

Stages are `starting`, `discovery`, `benchmark`, `simple_repair`, `fit` and `verification`.

A `benchmark_only` request, a mandate without training rights, or a model route that cannot train is admitted as `benchmark_only` with zero fits. Its reason is `benchmark_only_requested`, `training_outside_mandate` or `training_unavailable_for_route`.

### Holds and their next actions

| Reason | Next action | Next actor |
| -- | -- | -- |
| `standing_mandate_unavailable` | `enable_standing_mandate` | owner |
| `standing_mandate_paused` | `resume_mandate` | owner |
| `standing_mandate_withdrawn` | `renew_mandate` | owner |
| `standing_mandate_expired` | `renew_mandate` | owner |
| `standing_mandate_changed` | `start_new_run` | owner |
| `manual_learning_only` | `enable_standing_mandate` | owner |
| `standing_run_budget_exhausted` | `owner_raises_limits` | owner |
| `standing_fit_budget_exhausted` | `owner_raises_limits` | owner |
| `fit_ledger_unavailable` | `connect_fit_ledger` | operator |
| `plan_source_unavailable` | `connect_plan_source` | operator |
| `no_eligible_plan` | `wait_for_new_evidence` | worker |
| `plan_stale` | `refresh_plan` | owner |
| `trainer_submission_uncertain` | `reconcile_trainer_submission` | worker |

A run that could fit is held with `fit_ledger_unavailable` until an OPE-442 ledger is attached. Benchmark-only runs are not affected.

## Scheduled learning

The worker's schedule tick runs inside the existing `cycle-worker` unit. When the owner-authorized scheduler selection `learning-run-schedule.json` is on the worker volume, each iteration runs the learning-run tick. It does not also run the older direct cycle, so a scheduled intent cannot bypass the shared admission. Without that file the unit behaves exactly as before.

Each tick does the following:

1. Rechecks the scheduler's grant.
2. Reads the current standing mandate revision, including the owner-settings head. It keeps no copy of pause state, mode, fit count or caps.
3. Fences queued runs whose mandate was paused, withdrawn, expired or replaced. They become `refused` before any dispatch.
4. Records exactly one outcome: `admitted`, `no_change` or `held`.

A paused, withdrawn, expired or manual-only mandate admits nothing. While any run for the workflow is unfinished, including one awaiting review, the tick records `no_change` with `run_in_progress` or `awaiting_review`. With no new eligible plan it records `no_change` with `no_eligible_plan`. With no fits left it records `held` with `standing_fit_budget_exhausted`. Otherwise it submits one intent through the same admission as Slack and Zapier, using a key derived from the plan and operation. A restart or retry of the same tick therefore returns the same run.

The cadence is the unit's interval, at most one scheduled admission per tick, only for a new eligible plan, and only within the mandate's caps. The mandate does not yet store its own run frequency. That decision is recorded on OPE-559 and can be reversed by the owner.

## Worker interfaces for dispatch and discovery

These are local Python interfaces for the trusted worker. They are not HTTP routes.

* **Plan producer (OPE-562).** Call `publish_plan(directory, plan)` after a new eligible verified source revision. It seals the current plan for the workflow with the owner-local learning-run key. Readers ignore a plan whose seal, tenant, workflow or mandate revision does not match.
* **Executor (OPE-561).** Call `claim` to lease the oldest dispatchable run. Then use `advance` for stage and progress, `reserve_fit` for the single fit reservation, and `begin_submission`, `record_submission` and `reconcile_submission` for the trainer job. Finish with `finish` or `confirm_cancel`. Every call rechecks the grant, mandate, lease and cancellation first. An uncertain trainer submission holds the run until it is reconciled, and a new claim never resubmits blindly.
* **Fit ledger (OPE-442).** Configure `LearningFitLedger(dispatcher, actor=delegate, client=worker_identity, resolve=worker_request_resolver)` on `open_learning_runs`. The worker resolver rechecks the admitted run and supplies its frozen dispatch request. Bind the full plan document digest, routing receipt revision, connector model digest, and maximum seconds to the fit. Reservation sends nothing. The executor calls `FitDispatcher.advance` or `cancel`, and learning settlement checks the dispatcher’s independently verified seconds. The service always calls `lookup` before `reserve`, and never calls the ledger while it holds the grant guard.

## Install and authorize

Run these commands as the worker identity, with umask 077, against the private worker volume. The owner must be the mandate's registered owner.

```sh
python -I -S -B opentour-slack.pyz learning-runs authorize \
  --selection /var/lib/opentour-worker/learning-selection-slack.json \
  --tenant ORG --owner OWNER --actor OWNER --role owner --channel slack \
  --client slack-hosted-1 --workflow WORKFLOW_REF --mandate MANDATE_ID \
  --grant-ref slack-owner-1 --expires-at UNIX_SECONDS
```

For the schedule, authorize `--role scheduler --channel schedule` for the mandate's delegate, with `--selection /var/lib/opentour-worker/learning-run-schedule.json`. To revoke any connection at once, run `learning-runs withdraw --selection … --owner OWNER`.

Start a **separate** health.py instance for each hosted learning-run connection with `--learning-selection` and its own TLS secret directory, bearer credential, audience and configuration revision. Add `learning_tenant`, `learning_actor` and `learning_client` to that instance's private manifest. A listener refuses `--selection` and `--learning-selection` together. Route only the three exact learning-run paths to it.

## Not covered here

* Real dispatch to a GPU trainer and its evidence: OPE-561.
* Plan derivation from verified events: OPE-562.
* Installed composition of the qualified connector, verified frozen request resolver and fit dispatcher for the selected route.
* Slack App Home views and buttons: OPE-560. The Zapier template: OPE-549. A real Slack or Zapier install and provider receipts are separate installed-provider evidence.
* Release approval: OPE-529, OPE-413, OPE-403 and OPE-404.

Local tests use generated fixtures only. They are not evidence of an installed provider, a customer result or a GPU fit.
