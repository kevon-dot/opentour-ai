# EVE worker entry and separate evaluator (OPE-563, OPE-573)

Two shipped modules run the registered EVE question-and-evidence task through normal entry. Each
runs as its own process and reads only a private configuration file: an owner-only regular file in
a `0700` directory. Neither accepts an actor, tenant, key or credential on the command line.

```sh
# the customer worker
python -m opentour_cell.pilot.eve_worker run   --config /private/eve-worker/eve-worker.json
# the separate scorer: own identity, own custody, replays every arm itself
python -m opentour_cell.pilot.eve_evaluator evaluate  --config /private/eve-evaluator/evaluator.json
python -m opentour_cell.pilot.eve_evaluator recompute --config /private/eve-evaluator/evaluator.json \
    --verdict-key <key>
# the one staging gate: worker rebuild + aggregator reconcile + independent verdict
python -m opentour_cell.pilot.eve_worker stage --config /private/eve-worker/eve-worker.json \
    --verdict /private/exchange/eve-independent-verdict.json
```

Every command prints one bounded JSON line: states, digests, counts and fixed reasons. It never
prints a case, an answer, a prediction or a raw exception. The exit code is `0` on success
(`completed`, `reproduced`, `recomputed`, `stageable`) and `2` on any hold.

## What each step does

| Step | Module | What it checks or produces |
| --- | --- | --- |
| Register | `eve_worker` → `eve_task.EveRegistry` | Exact base, tokenizer and template fingerprints, decoding, destinations and capabilities. The same identity again is a no-op; a different one conflicts. |
| Freeze | `eve_replay.freeze` | Membership, content and answer-key digests, eligible and excluded cases, scorer identity. Written to the worker outbox. |
| Replay | `eve_replay.EveReplayWorker` | Baseline and simple repair on the base revision; journal rows carry scorer attribution and a keyed commitment per output, never the output. |
| Fit | `plan_guarded_fit` → `Preflight.submit_reserved_fit` | Zero fit on an inference-only target, an untrainable or unobserved exact revision, protected overlap, a protected answer in the payload, or a withdrawn grant. A dispatched job that is not complete is reported, never collected. |
| Candidate | `Preflight.collect_candidate`, `CandidateInference` | Lineage re-verified with the connector; the candidate arm keeps the simple arm's settings so the causal comparison is available. |
| Result | `build_result`, `ReplayJournal.settle` | One versioned result per manifest. A rerun is a `duplicate_ignored` settlement and runs no second fit. |
| Evaluate | `eve_evaluator.EveEvaluator` | Binding before any model call; serving identity; every replayed prediction checked against the worker's commitment (`verify_output`); its own verdict per case must equal the journaled one; totals recounted, never taken from the result. |
| Recompute | `EveEvaluator.recompute` | Re-scores from the predictions the evaluator holds privately, with no model call. |
| Stage | `eve_staging.stage_decision` | Requires the worker rebuild, `eve_aggregate.reconcile` and a `reproduced` independent verdict for exactly this manifest and result. Stageable is never a release or fit authorization. |

## Custody and evidence level

- The evaluator's custody directory is `0700` and holds its own key, sealed verdicts and private
  predictions. It must not overlap any worker data directory. That list is mandatory and covers
  the worker directory, the commitment key directory, the outbox and the backend.
- Private predictions are retention-limited: the OPE-528 default of 30 days, which a caller may
  only shorten. Retention sits inside the sealed record. `purge_expired` deletes expired records,
  and it also deletes any record whose seal no longer verifies.
- The evaluator reads the worker's per-run commitment keys read-only, so it can bind its own
  predictions to the worker's verdict rows. Predictions never leave its custody.
- A verdict the evaluator re-reads from custody is `locally_keyed`. The exported document the
  worker stages with is `digest_self_consistent_only`: without an OPE-566 evaluator key, the gate
  cannot tell it from a consistent forgery. The gate does require that such a document agree with
  the journal, the aggregator and the rebuild. Nothing here is proof to a third party.

## Not proven here

The only backend is the generated reference backend (`eve_reference_backend`). It is a scripted
stand-in model and an isolated file-backed trainer transport, and it refuses any destination that
is not `simulated:`. Its results carry the `generated` tier and `implementation_only` scope. These
remain separate evidence and are not reproduced by these tests:

- a real EVE serving endpoint and trainer;
- the OPE-558 public QLoRA smoke (GPU);
- the OPE-561 Slack-to-GPU run;
- an EVE standing learning mandate with cumulative budgets;
- packaging these commands into the `opentour-slack.pyz` kit.

The product test is `services/cell-runtime/tests/pilot/test_eve_product_entry.py`.
