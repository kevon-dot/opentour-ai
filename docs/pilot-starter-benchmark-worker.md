# Publish a starter benchmark state

Run the worker on the customer installation after the owner has generated a proposal or frozen adjudicated cases. Supply the absolute path to its existing private settings file and the admitted starter run reference.

```sh
python -m opentour_cell.pilot.starter_benchmark_worker "$private_settings" "$starter_run_ref"
```

The signed starter selection identifies the intake worker. Its current case grant must independently permit that worker to materialize this run. On success the command prints a JSON object containing the run reference and either `needs_owner_review` or `frozen_unscored`. Repeating the command recovers the same benchmark reference and version through ordinary authorized `get_run`. A denied invocation exits with status 2 and prints `starter_benchmark_unavailable` to standard error.

Scored adoption requires a separate authenticated completion receipt from the evaluation producer. This command handles proposal and freeze publication. The process tests use generated owner-private fixtures.
