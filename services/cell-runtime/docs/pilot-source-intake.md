# Bring a workflow trace and its independent outcomes into a pilot

Use this local procedure when a customer has approved one workflow, one decision field, a trace source and an independently adjudicated correction or outcome source. A custodian keeps files on the customer's approved machine. The source grant, grants for model training, and releases have separate owners and purposes.

## Prepare the two exports

The kit includes generated invoice examples in `generated/pilot-source-trace.jsonl` and `generated/pilot-source-correction.jsonl`. Use UTF-8 JSON Lines or CSV with a header. Both formats use UTC Unix seconds. In CSV, encode `features`, `incumbent` and optional `slices` as JSON cells. An empty CSV reference or a JSON null designates an unavailable business outcome. The first-ten profile accepts up to 5,000 input rows and 4 MiB per file.

| Export | Required fields |
| --- | --- |
| Both | `organization`, `workflow`, `field`, `source`, `event_id`, `policy_version`, `source_schema_version` |
| Trace | `customer_id`, `group_id`, `decision_at`, `features_available_at`, `features`, `incumbent`. An optional `slices` array holds predeclared cohort tags. |
| Correction | `outcome_id`, `adjudicator_id`, `facts_as_of`, `outcome_at`, `verified_at`, `reference` |

Use stable opaque event, customer, group, adjudicator and outcome IDs. Supply an explicit incumbent disposition such as `{"status":"automatic","label":"purchase"}`, `{"status":"review"}` or `{"status":"missing"}`. Features are a bounded map of approved names to printable strings. The trace timestamp is the latest availability of any supplied feature. The correction's `facts_as_of` is the latest point when all facts supporting that particular decision-time label were knowable. A ledger can verify an earlier action later. If later information was necessary to know the proper answer, the case is held. Each export has one named source, organization and field. A scope mismatch rejects the import.

## Discover, confirm and freeze

Keep the grant in the private directory of the selected training store. Each examined file must be owner-only, single-linked and regular within an owner-only directory. Start from `generated/pilot-source-grant.template.json`. Use the actual engagement ID, exact SHA-256 of each consenting export, named owner, allowed adjudicators and fields, approved as-of time, expiry and retention reference. The hash binds exactly the bytes the owner approved. Add a separate `training_attestation` when parameter updates have permission. An optional `training_expires_at` can shorten those rights. Supply named groups for semantically related or repeated events. A chronological cutoff uses later decisions as protected, while `{"mode":"heldout_customer","customers":["client-b"]}` protects the named customers. Set the split before inspecting errors.

The following calls run on the custodian's approved machine. Use private absolute paths and substitute the actual organization and receipt values. Run the distributed archive from the extracted kit directory with the supported Python 3.12.16 runtime.

```sh
python -I -S -B opentour-slack.pyz source --store /private/training.sqlite3 --organization generated-co discover \
  --grant /private/scope.json --traces /private/traces.jsonl --corrections /private/corrections.jsonl
```

Discovery returns the development-only repeating incumbent label → verified label, count, number of distinct groups and an opaque class ID. At least two development groups must contain the same mistake. Check that error pattern with the named owner. The owner's separate private confirmation file contains `discovery_revision`, `class_id`, `owner_id` and `attestation`. Carry over the exact discovery revision and selected class ID. The custodian supplies an opaque receipt for the owner's confirmation. The process relies on that custodian to verify who gave the decision.

```sh
python -I -S -B opentour-slack.pyz source --store /private/training.sqlite3 --organization generated-co freeze \
  --grant /private/scope.json --traces /private/traces.jsonl --corrections /private/corrections.jsonl \
  --output /private/intake.json --confirmation /private/confirmed.json
python -I -S -B opentour-slack.pyz source --store /private/training.sqlite3 --organization generated-co inspect \
  --intake /private/intake.json
```

Omit `--confirmation` to retain an evaluation-only discovery result with no approved recurring class. Missing or ambiguous corrections, conflicting trace versions, repeated use of one outcome for several events, changed policy, hindsight and late verification are counted with a reason for each withheld event. Identical export retries are counted once. Orphan outcome IDs are recorded separately from the trace population. Qualified cases remain auditable by logical event ID. Normalized feature-token equivalence and declared families form connected groups. A group spanning both sides is held in full. The intake does not infer arbitrary semantic similarity or verify that a customer supplied every applicable event. For chronological splits, development labels also must have become available before the cutoff.

## Use the frozen result

The owner receives a revision, exact byte and normalized source hashes, development and protected membership, and exclusion counts. Repeating identical input is idempotent. Reordering rows preserves normalized hashes and membership. It changes the raw-byte hash and requires the amended export to be authorized. Exact output paths are immutable.

The managed workflow inspects grant metadata without opening protected answers. Simple repairs use a separate development file. Parameter training additionally requires the unexpired training attestation and the confirmed class. Inference consumes only frozen case IDs and allowed features. A separate evaluator can then open the approved references and provenance. A structurally valid empty protected set leaves the evaluation without evidence. A trusted operator who already has supplied repair outputs can use `supplied_manifest` to produce the existing evaluation-only enrollment shape. Unknown predictions stay missing, and predictions for any outside event are refused.

## Withdraw and close

```sh
python -I -S -B opentour-slack.pyz source --store /private/training.sqlite3 --organization generated-co withdraw \
  --grant-attestation att-1111111111111111
```

This records a durable local hold against that source grant. Restoring an older export, grant or intake cannot re-enable its supported readers. Full engagement withdrawal also blocks access. The original exports, exact frozen file, grant, confirmation, derived files and backups are part of the normal customer custody inventory. Follow the retention agreement for copies, exports and backups. Custody controls apply to supported tools on this machine. Previously disclosed or customer-copied files require custodian follow-up.

### Save a policy-only starter in Slack

The workflow owner reviews their charter in **Add workflow → We’re starting from scratch** and selects **Save starter draft**. Reopening the same private route displays the workflow, intake run, draft and settings revision. The worker can resume an interrupted save against the same admission.

An installation operator maps the authenticated Slack owner to the existing registered mandate owner in the private Slack settings. Supply a current signed OPE-430 starter selection and the matching charter in the private worker directory. The two filenames must be local JSON basenames. The following values are generated examples.

```json
{
  "starter_setup": {
    "slack_owner": "UFOUNDER",
    "owner_ref": "registered-founder",
    "selection_file": "starter-selection.json",
    "charter_file": "starter-charter.json"
  }
}
```

The charter is part of the approved scope. Set the signed starter grant’s `setup_policy_revision` to the SHA-256 of the charter as encoded by `opentour_cell.pilot.operating_point.canonical`. The same grant names the permitted policy, its source revision, the custodian, owner and creation reference. Prepare both files under the worker’s private-file permissions.

```json
{
  "schema_version": "opentour.pilot.starter-charter.v1",
  "intended_decision": "Select the invoice review queue",
  "local_purpose": "Draft a local evaluation workflow",
  "verification_plan": "Have the reviewer independently check the next eligible invoice",
  "responsible_person": "verification-coordinator",
  "next_check": "2027-01-15",
  "verifier": null
}
```

Use the agreed next-check date and name the verifier when known. A policy-only save activates the local drafting scope. Live-source connection and training require separately authorized workflows.

### Connect the existing workflow to a live feed

Open private Slack Home, choose **Workflow settings**, then **Connect live
decisions**. Select an authorized committed-decision or independent-outcome
resource and choose **Save binding**. The screen returns the opaque binding
reference. Opening settings again shows current bindings. Saving the same
signed selection again returns the same reference. A decision feed can be
connected while independent verification is pending.

The worker uses the saved starter's workflow identity. To route a separate
custodian directly to the same admitted workflow, set the optional
`live_workflow_ref` in the private Slack settings file to that workflow's
opaque reference. The operator must have provisioned the existing signed
per-workflow live grant, engagement epoch, resource revision, lease and source
checkpoint. Its actors map must explicitly authorize the authenticated Slack
user ID. A user with live inspection can see current bindings and request
custodian action. Only a user with current live custodian authority can save.
The starter owner mapping grants no live role.

The intake worker needs its own intake grant to discover the resulting
`feed_binding_ref` and submit a typed live request. Source ingestion depends
on an installed authorized reader. The Slack settings action reads no feed
content and submits no intake, training, approval or release work.

### Show current training readiness

Place `readiness-selection.json` in the private worker directory. Use schema
`opentour.pilot.readiness-selection.v1`, the admitted `workflow_ref`, the
existing `worker_ref`, and `policy_file` (a private JSON basename, or null
when no simple-repair policy is configured). The current signed live grant
must independently permit that worker to inspect and ingest. The optional
policy identifies the same workflow, engagement and dataset revision and sets
the development population, repair rule and independent residual threshold.

After processing an authorized live page, the worker calls
`open_readiness(directory, tenant, workflow_ref, contexts=...).refresh()`.
An observed decision-context reader may support simple-repair assessment.
Missing decision-time input or committed-action classification keeps the
corresponding assessment unmeasured. The call persists the evidence-bound
readiness receipt and never admits a fit.

Private Home reads the persisted current receipt with the authenticated Slack
principal's live inspection grant and dataset aggregate permission. A pending
decision can request independent verification. With a signed custodian role
and permitted outcome resource, **Bind verifier** opens the existing private
binding form with only independent outcome choices. A training question can also show current readiness when the verified frozen
result explicitly identifies that workflow and its tenant, engagement, dataset
and revision all match. An unattributed result keeps its original answer.
A changed source, mandate or scope is rechecked before Slack receives either
projection. Reopening Home runs no assessment and creates no readiness record.

### Read the starter benchmark

Saving the private policy-only draft creates or reuses the versioned workflow
benchmark. The saved card shows the benchmark reference, version and recorded
state from the worker ledger. A newly admitted starter is a draft awaiting
owner adjudication. An approved policy revision submitted on the same creation
reference creates a successor on the same workflow. Reopening the earlier
authorized revision preserves its own admission and benchmark history.

A previously saved starter may predate the benchmark ledger. Opening its card
is read-only. **Create benchmark draft** explicitly retries the existing
canonical intake under the current signed grant. The existing workflow, run
and legacy starter receipt survive. Live-feed connections and admissions use
a separate evidence series. The owner can reopen the starter card to inspect
the same benchmark after a feed is connected.

### Showing an existing benchmark on live run status

To include benchmark metadata on a live run, the operator can select one previously
admitted starter or historical run in the same private state directory. Create
`benchmark-inspection-selection.json` with owner-only permissions and these fields:

```json
{
  "schema_version": "opentour.pilot.benchmark-inspection-selection.v1",
  "source_selection_file": "prior-intake.json",
  "benchmark_run_ref": "run-example"
}
```

Use the basename of the original OPE-430 selection and its actual admitted run
reference. The authenticated live actor must independently hold that original
grant for the same tenant, workflow and engagement epoch. Each status request
rechecks both authorities, the canonical admission and the current benchmark
version. A withdrawn grant, superseded run or missing benchmark leaves the
ordinary live status available without benchmark fields. Reading status does
not create a benchmark or dispatch work.
