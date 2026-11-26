# Start a customer-owned evaluation from MCP

Once the custodian has prepared and authorized one frozen workflow, a client can start its evaluation with one tool call. The worker runs on the approved customer machine. A separate read-only call returns progress and the first aggregate result. Source cases, corrections, model weights, people and local file paths stay outside the MCP response.

## Prepare and authorize

For first-release managed-worker qualification, use the extracted workspace kit with Python 3.12.16 and SQLite on an owner-approved Linux x86-64 host. The custodian stages the imported trace, corrections, scoped source grant and managed plan in the same owner-only directory. For a rehearsal use the generated example distributed with this kit. For a customer engagement record the actual source permission, permitted evidence recipient, retention terms and any separately authorized training and resource limits first.

Supply the actual private directory and organization. The authorization file must have a fresh filename inside the directory. Keep this file owner-only. The readiness check returns the canonical SHA-256 of the prepared plan. It validates the current source authority without opening protected answers.

```sh
pilot_dir=/private/customer-pilot
pilot_org=customer-organization
plan="$pilot_dir/managed-plan.json"
grant="$pilot_dir/workflow-permission.json"
python -I -S -B opentour-slack.pyz workflow-inspect --directory "$pilot_dir" \
  --organization "$pilot_org" --manifest "$plan" --authorization "$grant"
```

After checking the displayed revision, the custodian records the private authorization. Replace the two sample references with the exact reviewed revision and actual private custodian attestation. The expiry below is thirty minutes. Choose an expiry that covers the approved work. The command can also record a customer wait that the custodian actually measured using `--customer-wait-seconds`. Leave it unset when unknown. The local operator separately records what input or authorization the customer was awaiting.

```sh
revision=REVIEWED_64_CHARACTER_PLAN_SHA256
attestation=att-REPLACE_WITH_PRIVATE_HEX_REFERENCE
expires=$(python -c 'import time; print(int(time.time()) + 1800)')
python -I -S -B opentour-slack.pyz workflow-authorize --directory "$pilot_dir" \
  --organization "$pilot_org" --manifest "$plan" --authorization "$grant" \
  --plan-revision "$revision" --custodian-attestation "$attestation" --expires-at "$expires"
```

The authority is for local evaluation of this exact plan. It cannot purchase compute, activate a repair or confer model-training rights. The model path checks a distinct explicit grant. Any new consent or audience is a new review. The operator can remove the managed grant to stop further starts, processing and lookup. Follow the custody procedure to withdraw the underlying engagement and account for private files and prior disclosures.

## Start and retrieve

Configure this local stdio server in the permitted MCP client using absolute paths to its Python executable, kit, manifest, grant and private directory.

```sh
python -I -S -B opentour-slack.pyz mcp-workflow --directory "$pilot_dir" \
  --organization "$pilot_org" --manifest "$plan" --authorization "$grant"
```

Call `start_offline_evaluation` with exactly `{"plan_revision":"REVIEWED_SHA256","idempotency_ref":"request-001"}`. An accepted first call returns `queued` and an opaque `run_id`. Preserve that ID. Reconnects and concurrent retries of the same request return the same run. A reused idempotency reference with a different approved revision is refused. An unavailable or expired grant returns a held reason and creates no new run.

The operator services one admitted selection using its private configuration. The command exits after one job. Schedule it locally at the frequency approved by the custodian, or run it once for an attended evaluation. A queued job waits while the worker is stopped.

```sh
python -I -S -B opentour-slack.pyz workflow-worker --directory "$pilot_dir" \
  --organization "$pilot_org" --manifest "$plan" --authorization "$grant" --once
```

Call `get_offline_evaluation` with `{"run_id":"ORIGINAL_RUN_ID"}`. A `ready` response contains the evidence revision, the number of protected cases and the outcome. The result can favor a simpler repair, a model candidate or no change. A held comparison can return a reference to the hold evidence. A failed or held attempt cannot activate a repair. The private report and separate customer witness and business decision remain with the custodian.

| State | Meaning |
| --- | --- |
| `queued` | The worker has a durable accepted request. |
| `running` | A local worker has claimed this run. Status survives a client disconnect. |
| `ready` | The authorized local comparison has produced a qualifying initial result. |
| `held` | A required permission, evidence check or operating condition prevented an ordinary result. |
| `failed` | The worker failed or produced an invalid result. The operator checks the private run before doing further work. |

If a worker is interrupted, restart `workflow-worker --once` against the original private directory and grant. The operating system releases the dead worker's lease. A live, slow worker retains its lease. Recovery uses the original run and its frozen artifacts. A fresh explicit start can rebind the same run to a renewed grant for the identical plan. Revoked and foreign-source evidence remains held.

## Interpret elapsed time

The offer targets an initial result within one to two hours after scope, inputs and access are ready. The receipt records UTC Unix milliseconds for the custodian's readiness attestation, durable acceptance, first execution, finish and first qualifying result. `ready_to_result_ms` includes any local dispatch wait. The two-hour upper bound is 7,200,000 ms. A completed early run meets that upper bound. A hold or missing result reports an undetermined target. Customer planning and permission wait is recorded separately in seconds when known. Generated smoke timings characterize their own workload and host. Record timings and any target miss from each actual permitted pilot.
