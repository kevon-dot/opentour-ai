# OpenTour pilot workspace

Start with [the autonomous pilot guide](pilot-autonomous-cycle.md) to connect one permitted workplace source, bind training limits and supervise the customer-local worker. Each newly discovered set of verified corrections starts with the simpler repair. The supported model can train on the development partition when the residual errors and remaining budget qualify. The named reviewer sees the protected results in Slack before handing off that version.

Use [the two-route repair guide](pilot-managed-repairs.md) to rehearse both repair routes with generated data. For recurring private Slack invitations, enable `approval_requests` in the approved worker configuration. Use `slack CONFIG --flush-approvals` to dispatch outstanding requests and `slack CONFIG --unresolved-approvals` to list uncertain deliveries before reconciliation. The kit can run the offline software checks before a customer installs Slack or configures Zapier.

## Rehearse both modes

Use Python 3.12.16 on an owner-approved POSIX system with SQLite support. Unpack the ZIP into a new directory and compare its SHA-256 with the digest from the pilot operator. These commands run without credentials, a repository checkout or network access.

```sh
sha256sum -c SHA256SUMS
python -I -S -B opentour-slack.pyz --version
mkdir -m 700 generated-state
python -I -S -B opentour-slack.pyz evaluate --report-dir generated-state/supplied-report generated/readout-input.json
cmp generated-state/supplied-report/aggregate.json generated/expected-supplied/aggregate.json
cmp generated-state/supplied-report/brief.md generated/expected-supplied/brief.md
python -I -S -B opentour-slack.pyz data --db generated-state/training.sqlite3 --org generated-demo rehearse > generated-state/training-result.json
```

Open `generated-state/supplied-report/brief.md` and `aggregate.json` to see a comparison of two sets of supplied decisions. The training rehearsal fits `cpu_text_v1`, a multiclass perceptron, on six generated training examples and scores four held-out examples. Compare the frozen result with `generated/expected-training.json`. The local job ID will vary by installation. The sample reduces verified mistakes from two to one at four automated decisions out of four. Both arms miss the sample's 10% limit.

Use [the coverage and error rehearsal](pilot-operating-point.md) to compare a repair against two owner-set thresholds. The generated repair gets both automated answers right, yet it covers only 2 of 10 cases against an 80% minimum. The guide binds the agreement before scoring and repeats the owner-facing evidence in a fresh process.

## Repeat from frozen evidence

Read the generated job ID and start a new process to load its saved weights and recompute the predictions and report. The command checks the stored dataset, cohort, model and output identities. Rerunning a finished job shows its cached status. A score-only repeat performs a fresh calculation.

```sh
job=$(python -c 'import json; print(json.load(open("generated-state/training-result.json"))["job_id"])')
python -I -S -B opentour-slack.pyz data --db generated-state/training.sqlite3 --org generated-demo score-only --job "$job" --output-dir generated-state/first-repeat
python -I -S -B opentour-slack.pyz data --db generated-state/training.sqlite3 --org generated-demo score-only --job "$job" --output-dir generated-state/second-repeat
cmp generated-state/first-repeat/aggregate.json generated-state/second-repeat/aggregate.json
cmp generated-state/first-repeat/brief.md generated-state/second-repeat/brief.md
```

Each new directory also contains `repeat.json` with its source and digest bindings. A permitted recipient reruns or witnesses the comparison, checks the exact result and records that event privately. The workflow owner then records a dated business decision. Use the checklist in `operator-guide.md`.

Copy `templates/pilot-receipt.blank.json` for the witness and business decision. Keep the approved aggregate, brief, and decision-quality files intact so their hashes can be verified later.

## Onboard the customer through MCP

Use a separate owner-only directory for each organization. Use `generated/evaluation-only.json` as the shape for a comparison of supplied outputs. It needs zero training rows and grants no training capability. Use `generated/dataset.json` for approved bounded text-classifier training. The custodian selects the purpose, permitted fields and actions, opaque destination, retention terms and any eligible people. Held-out answers stay private. Only separately permitted training examples may appear in Slack.

The two commands below apply to a local generated training rehearsal. For a customer, substitute the actual operator-selected private paths, approved organization and opaque custodian attestation. Use the exact revision shown by the client's preview. The grant expiry controls enrollment permission. The owner records the separate agreement governing retention.

```sh
mkdir -m 700 onboarding-state
install -m 600 generated/dataset.json onboarding-state/dataset.json
expires=$(python -c 'import time; print(int(time.time()) + 1800)')
revision=$(python -c 'import json; print(json.load(open("generated/expected-training.json"))["dataset_revision"])')
python -I -S -B opentour-slack.pyz onboarding-authorize \
  --organization generated-demo --store "$PWD/onboarding-state/training.sqlite3" \
  --manifest "$PWD/onboarding-state/dataset.json" --authorization "$PWD/onboarding-state/grant.json" \
  --custodian-attestation att-0123456789abcdef --dataset-revision "$revision" --expires-at "$expires"
python -I -S -B opentour-slack.pyz mcp-onboard \
  --organization generated-demo --store "$PWD/onboarding-state/training.sqlite3" \
  --manifest "$PWD/onboarding-state/dataset.json" --authorization "$PWD/onboarding-state/grant.json" \
  --custodian-attestation att-0123456789abcdef
```

Configure the second command in the MCP client as a stdio server using absolute paths. It waits for an MCP client session. Call `preview_dataset_onboarding`, inspect the revision and scope, then call `confirm_dataset_onboarding` with that revision. A separate operator grant is required. The permanent registration remains accessible after reconnecting under the same permissions. For evaluation-only onboarding, use the supplied fixture, preview its own revision and issue a distinct grant. After enrollment, repeat with `data --db /private/workspace/training.sqlite3 --org ORG score-only --dataset DATASET_ID --output-dir /private/workspace/new-repeat`. The complete client setup and failure outcomes are in `pilot-onboarding-mcp.md`. The older fixed-result tool is available through `mcp-readout`.

Keep this offline rehearsal directory separate from the first Slack installation. The bundled training example binds the synthetic trainer `generated-reviewer`. For the Slack installation, follow `integrations/slack/README.md` to generate a fresh manifest bound to the actual trainer's Slack member ID before asking the custodian to approve its revision.

## Install inside Slack

The workspace administrator and custodian select a private, unshared channel, named actors, a readout recipient and permitted disclosure. Use `integrations/slack/manifest.json` to create the workspace app and `integrations/slack/worker.example.json` as a fill-in configuration. The sample expires all grants and starts with Zapier off. Keep the working copy inside the owner-only directory. The installation, pinned SDK, permissions, first real callback, wrong-channel check and optional Zap verification are documented in `integrations/slack/README.md`.

In the authorized channel, type `/opentour`. Browse or search the dataset and work in private native views. An evaluation-only dataset offers its supplied comparison. A training-enabled dataset can offer the bounded CPU profile, with up to 256 training rows, 128 held-out rows and a three-second CPU limit. The trainer confirms the specific proposal. An expired job lease stays uncertain until the operator reconciles the original job. The designated reviewer opens the evidence before selecting **Approve for readout** or **Request changes**. The local receipt binds the reviewer, audience, revision, model or supplied method, cohort and evidence shown.

The customer worker connects outbound through Socket Mode. A separately approved Zapier Catch Hook may receive only permitted receipt fields. Start the configured Slack worker with `--auto-flush-zapier` for automatic handoff, or run `--flush-zapier` for a single operator-initiated flush. Both use the same current permission and destination checks. Verify a matching downstream delivery in the owner's Zap before recording it as confirmed. The first-ten product provisions zero OpenTour-managed AWS services. The administrator controls the Slack workspace and any approved third-party destinations.

After a restart or uncertain request, use the [owner receipt recovery procedure](pilot-receipt-recovery.md) to discover outstanding receipts, inspect a held item and record independently verified downstream disposition. The listing reads private local state without sending an event.

## Withdraw and close an engagement

Use the same private directory and organization as the worker. The custodian identifies the agreement, support owner and due date. The command below is the form for an actual authorized withdrawal. Replace the uppercase fields with private references. Use `status` to inspect the current closeout without requesting a withdrawal.

```sh
python -I -S -B opentour-slack.pyz custody --directory /private/workspace --org ORG withdraw \
  --trigger withdrawal --agreement AGREEMENT_REF --custodian CUSTODIAN_REF \
  --support SUPPORT_REF --due YYYY-MM-DD
python -I -S -B opentour-slack.pyz custody --directory /private/workspace --org ORG status
```

Withdrawal durably stops new admission and queued handoffs first. Records previously disclosed to a recipient, backups and ambiguous in-flight sends require separate reconciliation. Use `reconcile`, then `record --help` to record an inventory item, its owner, deadline and private evidence. `purge-local` is a separate, explicitly authorized removal of live training-store rows. The custodian records the actual disposition of other files, secrets, SQLite copies, backups and external copies. `finish` requires support and custody evidence. A newly discovered obligation can reopen a retained closeout record. When the agreement permits only a closure receipt, the custodian may use `finish --receipt-only` after all obligations have been evidenced.

Keep the separate custody ledger when restoring engagement data. If an earlier training database and delivery journal are restored beside it, a matching withdrawal still blocks new registrations, training, scoring and queued sends automatically. Run `custody reconcile` to inventory the restored rows and record their disposition under the original agreement. An operator who must also recover the custody ledger should restore the current withdrawal record before reconnecting the worker. Use a separate location for each engagement.

For support, give the named operator the version, digest and a redacted diagnostic through the agreed private route. Hold disputed outputs and use the previously verified kit when authorized. `PROVENANCE.json` identifies the source commit. From that checkout, maintainers can reproduce this ZIP with `python -I -S -B tools/build_slack_pilot_kit.py OpenTour_Slack_Workspace_Kit.zip` and a fresh output path.
