# Run one customer-local repair comparison

A workflow owner chooses one recurring decision, its authoritative outcome, the minimum automatic coverage and maximum wrong-automatic rate. OpenTour tests a credible simpler change first. A rule that meets the agreed requirements can ship immediately after the protected comparison and owner decision. A confirmed recurring error can instead qualify one supported open-weight model for supervised tuning on approved local compute. The held-out evaluator can recommend the rule, the model, the incumbent, or a hold.

## Rehearse the rule in the distributed kit

Use a new directory on an approved POSIX machine with Python 3.12.16. These commands use generated data and separately stored operator permissions. The example prepares twelve development cases and four protected cases. It does not contact a provider, authorize a model fit, post to Slack, or count a customer acceptance.

```bash
umask 077
python -I -B generated/prepare-repair-example.py "$PWD/demo-rule" --route rule
kit="$PWD/opentour-slack.pyz"
work="$PWD/demo-rule"
run_workflow () {
  local command="$1"
  shift
  python -I -S -B "$kit" "$command" --directory "$work" --organization generated-co \
    --manifest "$work/managed-plan.json" --authorization "$work/workflow-permission.json" "$@"
}
```

The helper above uses Bash. Under Bash, inspect the selected revision and issue the owner's permission from a separate operator shell. Keep real attestations and grants private.

```bash
revision=$(run_workflow workflow-inspect | python -c 'import json,sys; print(json.load(sys.stdin)["plan_revision"])')
expiry=$(python -c 'import time; print(int(time.time())+1800)')
run_workflow workflow-authorize --plan-revision "$revision" \
  --custodian-attestation att-aaaaaaaaaaaaaaaa --expires-at "$expiry"
```

Connect an MCP client to the same kit command and call the tool once.

```sh
python -I -S -B opentour-slack.pyz mcp-workflow --directory "$work" \
  --organization generated-co --manifest "$work/managed-plan.json" \
  --authorization "$work/workflow-permission.json"
```

Call `start_offline_evaluation` with the inspected `plan_revision` and an `idempotency_ref` of your choice. Run `run_workflow workflow-worker --once` on the approved operator machine. The client can call `get_offline_evaluation` with the returned `run_id`. Repeating the start with the same request returns the same run. Record the observed elapsed time from readiness to first result. The proposed 1–2-hour target depends on the ready inputs and approved machine. A live customer must separately verify their own workload and timing.

The generated rule uses the event type, fixes two wrong decisions on four protected examples and makes zero model-fit calls. A real owner can supply the traces and independent corrections through the source importer, choose the feature available at decision time, and declare or consent to deriving the rule. An unavailable source, unconfirmed recurring error, insufficient protected cohort or missing required operating evidence produces a hold. [Protected comparison and measurements](protected-comparison.md) · [Source intake](pilot-source-intake.md) · [Managed MCP and worker recovery](pilot-managed-mcp.md)

## Rehearse the real model

Create a different private directory with `python -I -B generated/prepare-repair-example.py "$PWD/demo-model" --route model`. Provision the documented optional CPU runtime and stage the pinned public model and tokenizer under that directory's `model-base`. The simple repair leaves recurring errors in this generated cohort. The operator records the model rights, accepted local resource cap, permitted features, labels, destination and payer before the job starts.

```sh
work="$PWD/demo-model"
expiry=$(python -c 'import time; print(int(time.time())+1800)')
python -I -S -B opentour-slack.pyz repair --directory "$work" --organization generated-co \
  --manifest "$work/managed-plan.json" permit-model --trainer generated-trainer \
  --payer generated-owner --rights att-9999999999999999 --destination owner_local_store \
  --feature description --label refund --label sale --max-seconds 120 --expires-at "$expiry"
```

Set `OPENTOUR_TRAINING_PYTHON` on the worker to the approved interpreter with the pinned numeric packages. Repeat inspection, the separate workflow permission, the MCP start and the worker command with this new directory. The local model changes adapter parameters on development examples. The evaluator compares frozen predictions on held-out cases. An absent or expired model grant cannot authorize a fit. Working configurations provision zero OpenTour AWS services. [Optional local model profile](pilot-open-weight.md)

## Review, transfer and close

The owner receives the complete private comparison and may authorize a customer-specific repair bundle. Run `repair --directory DIR --organization ORG --manifest PLAN prepare-handoff --run-id RUN` to stage the exact chosen rule or adapter, signed-off operating assumptions and compatible fallback. Exercise a second process that verifies the package, predicts using its version, replays the benchmark and runs the fallback. The receiver records a witness and the workflow owner records the business decision. [Versioned repair handoff](pilot-repair-handoff.md)

To review aggregates in Slack, predeclare `slack_review` in the plan with its exact team, channel and audience, `expires_at`, a scoped `owner_attestation` and `aggregate: true`. The custodian grants it with the `repair ... permit-slack` command. Only authorized reviewers in the declared private workspace can find the managed run. A review decision authorizes presentation to that audience. The approved local artifacts and the separate customer decision govern subsequent use. Zapier can send the optional permitted receipt. [Slack installation and callbacks](integrations/slack/README.md)

After withdrawal, reconcile local sources, protected results, private model copies, encrypted handoffs, grants, external copies and any unresolved receipts against the owner's retention terms. A previously exported offline copy requires follow-up with its recipient. [Delivery receipt recovery](pilot-receipt-recovery.md)
