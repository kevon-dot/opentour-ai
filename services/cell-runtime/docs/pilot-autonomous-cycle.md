# Let OpenTour find a recurring error and prepare a repair for Slack

A customer can authorize one local worker to find the approved decision records and independent corrections, test the low-cost repair, and train a model on eligible records within a standing budget. The customer chooses the workspace, private channel, reviewer and permitted actions at setup. A qualifying result goes to the named Slack reviewer. The operator does not need to launch or approve each fit.

## Establish the boundary once

1. Register the customer and an active local engagement. Use a mode-0700 directory owned by the customer and mode-0600 grants. Provision a read-only MCP workplace connection with approved trace and correction namespaces, published policy version, effective-time fields and the allowed decision-time projection. The connection pins the local reader executable, provider name and exact configuration. The reader must implement the workplace projection contract. [Workplace discovery and acquisition](pilot-workplace-context.md)
2. Write a private standing agreement. Its fields are `connection_revision`, `incumbent_revision`, `terms`, `rule`, `handoff`, `release_review` and `slack_review`. The connection revision is the SHA-256 of the canonical JSON object. Use `operating_point.digest` from the source package, or hash the UTF-8 encoding of `json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)` after parsing the file. Hash the completed agreement the same way. Declare the rule, minimum automated coverage, maximum material error, uncertainty limits and critical slices before running. Record an exact incumbent revision and compatible manual or versioned fallback. [Managed repair plan](pilot-managed-repairs.md)
3. Write `release_review` with `team`, `channel`, `reviewer`, `audience` and `provider:"zapier_slack"`. The matching `slack_review` declares `team`, `channel`, `audience`, `aggregate:true`, an actual owner authorization reference in `owner_attestation` and an approved `expires_at` in UTC Unix seconds. Keep it within the source and mandate validity. The worker reads the frozen agreement and carries that exact destination into the review. A separately enrolled native Slack route can use an explicitly pinned `provider:"slack"`.
4. Have the customer enroll the standing mandate. Set `mode:"autonomous"` and bind the actual owner, distinct delegate, reviewer, trainer, payer, workflow, field, connector provider, trace source, correction source, decision-time fields, labels, allowed error pairs and agreement revision. The row limit counts both source exports. Set per-run and total limits, expiry and maximum runs. A rule-only agreement allows inspection/evaluation and zero fit, time and cost budgets. The model branch also requires training permission, a supported pinned profile, allowed model fields, a positive fit/time budget and a permitted local base. The first-ten path uses existing approved local compute and a zero incremental spend cap. [Standing learning mandate](pilot-learning-mandate.md)
5. Place the worker's eight-field `cycle.json` beside the workplace connection and standing agreement. These filenames are resolved in the customer directory. Keep Slack credentials and model weights out of the config.

```json
{
  "schema_version": "opentour.pilot.autonomous-cycle.v1",
  "organization": "generated-co",
  "engagement": "replace-with-enrolled-engagement",
  "mandate_id": "generated-mandate",
  "delegate": "generated-worker",
  "connector": "generated-reader",
  "connection_file": "workplace-connection.json",
  "agreement_file": "standing-plan.json"
}
```

## Run and repeat

Use Python 3.12.16 and SQLite on the owner-approved Linux x86-64 host selected for first-release qualification. One invocation performs a complete bounded scan, from metadata discovery through the protected decision. The supervised local worker repeats this same operation at the configured interval. The customer operates that worker without AWS. Zapier coordinates the approval message and callback through the enrolled Slack integration. Follow the [Zapier and Slack approval setup](pilot-zapier-approval.md) to install the private worker and approvals.

```sh
python -I -S -B "$KIT" cycle --directory "$PILOT_OWNER" --organization "$ORG" \
  --config "$PILOT_OWNER/cycle.json" --once

python -I -S -B "$KIT" cycle-worker --directory "$PILOT_OWNER" --organization "$ORG" \
  --config "$PILOT_OWNER/cycle.json" --interval 300
```

The worker lists the approved MCP resource catalog, requires a unique pair and checks row capacity before requesting its two resources. A repeated unchanged catalog version uses its private pinned snapshot. A fixed connection keeps its original as-of cutoff. To admit later data without a new grant, the custodian can pin `watermark_mode:"provider_verified"` at enrollment. Each newly versioned resource must publish a matching `covered_through` value in its catalog and content. The cycle uses the earlier of the two verified watermarks, subject to current source and mandate validity. Serve newly eligible protected cases in the authorized projection. Previously consumed cases or families hold the comparison. Changes in connector configuration, namespaces, terms or recipient need fresh owner authority. A new provider version must pass fingerprint, row, run, prospective split and protected-cohort checks. Simultaneous scans of one mandate share an immutable admission and run ID.

The independently verified correction and decision timestamps determine which rows qualify. A worker may confirm exactly one discovered recurring class covered by the customer's predeclared label pair. The intake records `standing_customer_mandate`, its run and the machine confirmation. The owner's original connection authority remains visible. Manual or feedback-only enrollment, an ambiguous class, withdrawn source permission or a consumed holdout stops the affected branch.

A successful inexpensive rule skips the model. When development evidence leaves a qualified residual and the standing mandate permits training, the worker reserves and claims compute once, fits the genuine adapter locally, saves its version and measures the actual runtime. Only permitted development examples reach the model. The evaluator opens the separate frozen comparison after the arms and predictions are bound. An interrupted fit with uncertain completion keeps its reservation. The worker may reuse an independently verified saved artifact. A human must inspect any ambiguous remaining work before further authorization.

## Interpret the reply

| State | Operator action |
| --- | --- |
| `pending_slack_review` | Open the exact private review request, inspect the incumbent, rule and any model on the same protected cases, including errors, coverage, tradeoffs, limits and fallback. The named reviewer requests changes or approves the displayed revision. |
| `reviewed` | Read `review_state` on the existing approved or changes-requested revision. The customer separately controls artifact handoff and any production rollout. |
| `held` | Use the safe `reason` and any returned source/run revision. Correct the owner-side evidence, authorization or fresh-cohort gap. A completed comparison may legitimately retain the incumbent or report insufficient evidence. |

The review receipt identifies work waiting for the Slack/Zapier delivery worker. A pending receipt proves local preparation. Provider delivery and the human's decision have their own receipts. Data, protected answers, trained artifacts and customer signatures stay in the agreed private custody. Follow the [recipient handoff guide](pilot-repair-handoff.md) after the exact human approval. Removing the source grant, withdrawing the mandate, or reaching its cutoff blocks subsequent work and review access. Account for any prior offline handoff under its separate custody terms.
