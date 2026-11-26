# Workflow benchmark admission (OPE-505)

This backend slice creates durable benchmark identity from an already-authorized
OPE-430 intake. It is not the complete OPE-505 materialization or product flow.
The current native issue, OPE-411 parent and shared first-ten implementation
contract govern acceptance; OPE-378/384 own source joins and frozen comparisons,
OPE-513 owns starter adjudication and OPE-539 owns starter scoring.

## Composition and authority

Wrap `DurableIntake` with `BenchmarkIntake` in the worker-owned factory. The
existing `discover_sources`, `submit_intake` and `get_run` wire shapes are
unchanged. The wrapper creates a private `workflow-benchmarks.sqlite3` only for a
starter or historical admission. The admitted tenant, current engagement epoch,
workflow, source/policy revisions and grant identity come from the existing
authority guard. No wire field grants access.

`DurableIntake.admitted_work(actor, run_ref)` pins that current authority and an
existing read-only intake transaction. Consumers may write their separate
metadata ledger inside it, but must not reacquire the authority's writer lock.
The wrapper reserves the draft before returning admission to a dispatcher.
If the process stops after durable admission but before draft creation, repeating
the canonical intent recovers the same workflow, run and benchmark identity.

The ledger does not store source records, protected answers or a training payload.
Fresh authorized source/policy intent produces one successor in the same workflow.
The prior reference remains inspectable as-of under explicit current benchmark
inspection rights; its current state is `superseded`. Workflow labels are absent
from the identity calculation.

`WorkflowBenchmarks.observed_workflow(BenchmarkScope)` is a read-only identity
port for the signed live authority. Its caller must already hold current
authority and supply the trusted tenant, current engagement epoch and workflow.
An absent, foreign or old-epoch workflow returns false. This grants neither feed
access nor benchmark inspection and never creates a workflow from live input.

Live `get_run` exposes an existing current benchmark only through a separate
`BenchmarkInspection.guard`. Feed authorization alone does not imply benchmark
visibility. Live submit, refresh and get-run never write the benchmark ledger.
The legacy authority's existing benchmark projection remains available as the
fallback, and previous runs can still be read without creating new records.

## Materialization boundary

`BenchmarkMaterializer` is a worker-only producer port, not evidence that a
historical or starter producer is installed. An implementation must actually
recheck its source and evaluation grants, use the existing grouped source joins,
reserve the protected comparison population, and return a verified immutable
definition/result receipt. Missing permissions, inputs or producers must hold.
It must be idempotent for the durable run because retries may invoke it again.

The adapter invokes the producer outside the intake writer guard, then pins the
same current intake again before publishing its result. Revocation between those
steps prevents publication. A frozen definition cannot change when scored; an
unscored receipt has neither result reference nor result digest. A scored version
is immutable. No default model digest, score or evaluation permission is minted.

## Acceptance map and evidence limits

Generated tests in `test_workflow_benchmarks.py` exercise actual private SQLite
ledgers and the real durable intake/stdio implementation:

| Native requirement | Coverage in this slice |
| --- | --- |
| Equivalent keys, concurrency, restart retain one draft | Concurrent canonical submissions, reopened ledger, two fresh stdio processes |
| Crash between admission and dispatch | Injected interruption before draft persistence, recovery under a fresh operation key |
| Reused key with changed intent conflicts | Current durable intake rejects changed policy intent |
| New revision makes one successor with readable parent | Fresh policy revision, current supersession and explicit as-of inspection |
| `test_live_admission_leaves_benchmark_unchanged` | Two live feed revisions and restart preserve all benchmark rows and reference/version/state |
| Live must not create workflow/benchmark identity | Live-only case leaves the benchmark file absent; exact tenant/epoch identity negatives |
| Unscored starter must not invent result | Null result fields; partial result metadata rejected; frozen definition change rejected |
| Current rights, tenant and role boundaries | Revocation during producer, foreign tenant and evaluation-only intake denial |

The test materializer deliberately returns generated receipts to test the port;
those receipts are not actual OPE-513 adjudication or historical comparison
evidence. Complete OPE-505 acceptance still requires normal factory/worker and
Slack integration, actual standing-mandate historical freeze with recorded
predictions and zero fits/model credentials, owner-adjudicated starter production,
specific missing-input holds, denominator/split reconciliation, protected-answer
exclusion, existing historical result qualification and an authorized manual rerun
on the same frozen definition. This slice does not close OPE-505 or establish any
installed-provider/customer result.

Run scoped software verification with the pinned Python 3.12.16 environment:

```sh
OPENTOUR_UNIT_GUARD=1 PYTHONPATH=packages/contracts/common/python-unit:services/cell-runtime/src:. \
  .venv/bin/python -m pytest -q \
  services/cell-runtime/tests/pilot/test_workflow_benchmarks.py \
  services/cell-runtime/tests/pilot/test_intake_mcp.py
```
