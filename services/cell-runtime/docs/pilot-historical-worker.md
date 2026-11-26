# Historical benchmark worker entry (OPE-505)

Run the existing historical producer for one already-authorized durable intake
run on the customer worker. This entry locates private files; it does not enroll
a mandate, create an intake, grant evaluation rights or send Slack messages.

The owner places this configuration in the same private directory as the existing
intake selection, source exports, pinned measurement policy and worker databases:

```json
{
  "schema_version": "opentour.pilot.historical-worker.v1",
  "selection": "historical-selection.json",
  "traces": "trace.jsonl",
  "corrections": "corrections.jsonl",
  "agreement": "benchmark-policy.json"
}
```

All four paths are distinct basenames in the current directory. The directory
and files must be private to the worker owner. Configuration is bounded to 64 KiB,
opened without following links, and rechecked during producer authorization.
The existing source reader bounds and validates the source exports. Actor, tenant,
epoch, source hashes, policy revision and evaluation permission come from the
selected current authority and existing admission, never from configuration fields.

Invoke with the opaque run reference returned by durable historical intake:

```sh
python -I -S -B opentour-slack.pyz historical-worker \
  --config /absolute/private/historical-worker.json --run-ref <admitted-run>
```

The command uses `BenchmarkIntake.materialize` with `LocalHistoricalBenchmark`.
Its existing operation ledger serializes dispatch and recovers a produced receipt
after interruption. Repeating a completed run returns its verified metadata;
uncertain work is held without blindly repeating a score. Repair comparison and
recorded-baseline policies retain their distinct producer behavior. Baseline
evaluation requires recorded predictions and standing evaluation rights, with
zero development cases, model credentials and fits permitted by that intent.

Standard output is one JSON object containing a completion or hold status.
Successful output includes only the opaque run and benchmark reference, version
and state. Exit code 0 means completed; code 2 means held. The command exposes an
allowlisted reason such as `recorded_baseline_predictions_required`,
`source_export_digest_mismatch` or `benchmark_materialization_unresolved`, and
never emits raw file paths, tracebacks, source rows or protected answers. A missing
input leaves the draft held. A later changed source/policy needs its own authorized
intake intent; it is not a reason to redispatch an uncertain prior operation.

Generated tests run the actual command in fresh processes, verify one protected
result across restarts and zero reserved fits, recover a persisted result after
interruption without scoring again, and deny changed sources/policy, withdrawal,
foreign runs and unsafe configuration. These are local software checks. Normal intake
dispatch scheduling, starter OPE-513/OPE-539 production, Slack exposure and full
historical/manual-rerun qualification remain acceptance work; this entry does not
close OPE-505 or establish customer/provider evidence.
