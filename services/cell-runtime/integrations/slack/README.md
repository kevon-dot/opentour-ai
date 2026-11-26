# Set up the OpenTour pilot in Slack

This guide starts with a customer-approved machine and an administrator. The worker connects out through Socket Mode. The customer machine holds the cases, candidate, evidence and decision journal. Anything displayed in a Slack message or private modal enters that workspace and its retention policy. The offline comparison can operate with Slack and Zapier disabled.

## Establish the workspace and people

1. Ask the workflow owner to name the engagement custodian, Slack workspace administrator, training operator if applicable, one person accountable for the readout decision, and its permitted recipient. The owner approves which dataset revision, aggregate fields and optional training examples those people may see. Record the scope, expiry and retention privately. Begin with generated data.
2. Have the administrator choose an existing approved workspace or create a dedicated workspace using Slack's onboarding. Invite the named people and create a **private** channel. Use a channel that has never been shared through Slack Connect, is active and has only approved members. The administrator can also create a second private rehearsal channel for the denial check. [Create a workspace](https://slack.com/help/articles/206845317-create-a-slack-workspace) · [Create a channel](https://slack.com/help/articles/201402297-Create-a-channel)
3. The administrator obtains the exact workspace ID, private-channel ID and member IDs. The Slack web client URL contains the workspace and channel IDs. In a member's profile, open the menu and choose **Copy member ID**. For a simple first rehearsal, use the named recipient's member ID or the approved private channel's ID as the audience. The reviewer and audience can differ. Record IDs privately.
4. In Slack's app management console choose **Create New App → From a manifest**, select that workspace, and import `manifest.json` from this directory. Install the app into the workspace, completing any administrator approval. Copy its app ID from **Basic Information**. Add the installed app to the approved private channel. The bot uses `commands`, `groups:read` and `chat:write` for slash commands, privacy/membership checks and an optional generic launcher. [Slack app manifests](https://docs.slack.dev/app-manifests/configuring-apps-with-app-manifests/)
5. Under **Basic Information → App-Level Tokens**, create a token with `connections:write`. Store it as `SLACK_APP_TOKEN` in the approved worker secret store. Store the installed bot token from **OAuth & Permissions** as `SLACK_BOT_TOKEN`. Check that Socket Mode and interactivity are enabled. The tokens belong in the worker environment. Keep them out of Slack, command arguments, shell history, configuration examples and support tickets. [Slack Socket Mode setup](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/index.html)

## Customize the workspace

Set `workspace_title` (1–48 printable characters) and optional `workflow_label` (1–64) in the private worker configuration. An example uses “Finance operations” and “Invoice classification.” Use ordinary display text. Mention, channel and link delimiters are rejected. The title and workflow are visible in the authorized member's personal App Home and workspace modal. The Slack administrator can set the installed app's workspace-wide name, icon and description in the Slack app settings. [App Home](https://docs.slack.dev/surfaces/app-home/) · [Home events](https://docs.slack.dev/reference/events/app_home_opened/)

The app publishes a personal Home when a member opens the **Home** tab. **Review pending repair** opens the exact current candidate when the named reviewer has an outstanding release decision. Other aggregate readers see **Review results**. **Browse data** and **Ask OpenTour** open private views. **Preview a repair** appears when the member has the tune role and a current named launch grant on an allowed dataset. Each button checks access again. The Home contains navigation and the operator's labels. Reports, source IDs and customer examples stay in their permitted private views. A withdrawn member receives a neutral Home on the next open. The app acknowledges actions immediately and displays **Checking…** while it verifies permissions. Managed execution shows its actual queued, running, completed or held state with **Refresh status**.

**Ask OpenTour** and `/opentour prompt` accept bounded commands such as `review`, `search FIELD`, `status RUN`, `repair SOURCE` and, after inspecting one result, `ask coverage`. The **What got worse?** button shows verified same-case regressions and fixes, the number of comparable cases, and the excluded count. Other questions cover errors, coverage and audience. The private search is limited to 64 characters. Each approved managed run requires a separate **Queue workflow** confirmation on its preview. The trained candidate also requires a separate human decision about its exact protected evidence before handoff.

After inspecting a managed comparison, the named reviewer can select **View comparison charts**. Slack addresses one ephemeral message to that person in the approved private channel. The first chart compares coverage and verified errors for the methods actually evaluated. The second, when a repair qualified and comparable cases exist, shows fixes and newly wrong cases against the incumbent. The same numbers and denominators are available as text in the private modal, including when the client cannot draw the charts or Slack cannot confirm delivery. Chart clicks leave the decision pending. The private evidence view also shows the frozen workload and benchmark figures when available, including the basis for timing and cost. [Slack chart blocks](https://docs.slack.dev/reference/block-kit/blocks/data-visualization-block/) · [Ephemeral delivery](https://docs.slack.dev/reference/methods/chat.postEphemeral/)

## Request human approval after an autonomous run

1. In the autonomous mandate and the managed plan, set the exact source rights, named reviewer, workspace, private channel, audience and release provider. Separately authorize private aggregate review. Configure the same reviewer and recipient in this worker and grant the named person both inspect and review roles on the source, including aggregate access. The workspace administrator must invite the app and reviewer to the approved private channel.
2. Set `approval_requests` to `true` on this worker when the owner permits invitations. Choose `approval_provider` equal to the immutable plan's provider. Use `zapier_slack` for **Slack Request Approval in Zapier**, or `slack` for the native named reviewer's buttons. The sample defaults to the native provider and keeps invitations disabled. A readout receipt and an invitation each have a different purpose from approval of a candidate.
3. When a protected result qualifies, the cycle stages one private pointer. A connected worker checks it on startup and every 30 seconds. To process just one pass, run `pilot-venv/bin/python -I -B opentour-slack.pyz slack /private/customer-pilot/worker.json --flush-approvals`. The selected private channel receives one generic message with **Open evidence**. Only the named reviewer can open that message's exact evidence and recipient. The candidate remains held until an explicit handoff decision.
4. On the native route, **Approve repair for handoff** or **Request changes** applies to the exact inspected candidate. On the Zapier route, the reviewed candidate is staged for the separately configured approval workflow after inspection. When the owner enables the plugin and supplies its authorized hook through `OPENTOUR_ZAPIER_APPROVAL_HOOK_URL`, the same supervised Slack worker sends staged plugin requests, then imports signed returns from the private `zapier-decisions` inbox. The provider return transport and signing key are installed once in the customer setup. `--flush-approvals` runs the same cycle on demand. A missing outbound hook still allows an authorized signed response already in the inbox to complete. A readout-only approval permits presenting the report. The customer controls deployment separately.
5. To inspect uncertain request delivery locally, run `pilot-venv/bin/python -I -S -B opentour-slack.pyz slack /private/customer-pilot/worker.json --unresolved-approvals`. After an ambiguous result, the worker avoids sending a duplicate. An authenticated click from the named reviewer on the original Slack message can reconcile its receipt. The custodian handles an unresolved original message during closeout.

## Prepare one private worker

For first-release live managed-worker qualification, use Python 3.12.16 and SQLite on an owner-approved Linux x86-64 host. Start in the extracted kit directory. Replace `/private/customer-pilot` everywhere with an approved absolute path. Use a separate mode-0700 directory and worker for each organization. Obtain the released or review-candidate kit's SHA-256 through the operator's approved handoff and verify it before installation.

```sh
umask 077
install -d -m 700 /private/customer-pilot
cp integrations/slack/worker.example.json /private/customer-pilot/worker.json
chmod 600 /private/customer-pilot/worker.json
python3.12 -m venv pilot-venv
pilot-venv/bin/python -m pip install --only-binary=:all: --require-hashes -r integrations/slack/requirements.txt
```

On a machine with a preapproved wheelhouse, add `--no-index --find-links /approved/wheels` to the pip command. The package needs the pinned Slack SDK for the live worker. Offline enrollment and scoring use the standard library. In a source checkout, the installation files are in `services/cell-runtime/integrations/slack/`. The ZIP retains `integrations/slack/`. The following commands use the ZIP entry point.

Edit the copied configuration privately. Use the actual `organization`, `directory`, Slack `team`, `app`, `channel`, `reviewer` and `recipient`. In `users`, key each actor by their Slack member ID. Grant only their named dataset IDs and a future owner-approved Unix `expires_at`. The template's past or zero expiry keeps access closed.

| Configuration | Effect |
| --- | --- |
| `inspect` role | Search that actor's registered datasets and inspect allowed job status. |
| `preview` | Separately permits the owner-selected training examples and columns in a private modal. Evaluation-only enrollments expose no examples or training action. |
| `tune` role | Permits proposals for a training-enabled dataset. The confirming trainer must also match the original proposal and frozen reviewer grant. |
| `aggregate` and `review` | Aggregate permission permits private evidence. The named reviewer with the review role can decide on presentation after inspecting the exact revision. |
| `channel_aggregates` | Controls summary counts on the personal workbench. The optional channel launcher contains a generic button. |
| `zapier` and `zapier_disposition` | Leave both false for the initial rehearsal. Configure the separate outbound consent below to use them. |

Check the configuration locally. This command validates the file and IDs. The live worker separately verifies the Slack workspace, app and private-channel access.

```sh
pilot-venv/bin/python -I -B opentour-slack.pyz slack /private/customer-pilot/worker.json --check-config
```

## Rehearse one authorized and one denied callback

Start in a fresh owner-only Slack pilot directory. Keep the kit's standalone `generated-state/` and `onboarding-state/` rehearsals separate. Their training fixture names `generated-reviewer`. For Slack, the approved real trainer's member ID must be in the training manifest **before** the custodian previews and grants that revision. If this Slack-bound store is already enrolled, inspect its current catalog and grants. Reuse the exact approved revision instead of seeding or registering new bytes under the same dataset ID.

In the extracted kit directory, set the following values to the organization and private directory in your worker configuration. Replace the illustrative member ID with the actual trainer. For this generated rehearsal, create both input files in the fresh directory with owner-only permissions. These commands generate manifests. Enrollment follows separately through MCP.

```sh
pilot_dir=/private/customer-pilot
pilot_org=generated-demo
trainer_id=UREVIEWER
pilot_python="$PWD/pilot-venv/bin/python"
pilot_bundle="$PWD/opentour-slack.pyz"
(
  set -euC
  umask 077
  test ! -e "$pilot_dir/training.sqlite3"
  "$pilot_python" -I -S -B "$pilot_bundle" data --db "$pilot_dir/training.sqlite3" --org "$pilot_org" generated-fixture --reviewer "$trainer_id" > "$pilot_dir/training.json"
  "$pilot_python" -I -S -B "$pilot_bundle" data --db "$pilot_dir/training.sqlite3" --org "$pilot_org" generated-evaluation-fixture > "$pilot_dir/evaluation.json"
)
```

Use the kit's `pilot-onboarding-mcp.md` to configure two local stdio connections in your MCP client. Both use the same `pilot_org` and `$pilot_dir/training.sqlite3`. Give each its own manifest and grant path.

| Connection | Input in the private directory | Grant file in the same directory | Dataset ID |
| --- | --- | --- | --- |
| Training | `training.json` | `training-grant.json` | `generated-intake` |
| Evaluation only | `evaluation.json` | `evaluation-grant.json` | `generated-supplied` |

For a Codex MCP client, the following example registers both commands. Use the equivalent stdio configuration in another approved client. The attestation below is an illustrative receipt for generated data. Substitute the custodian's private receipt for a real engagement.

```sh
custodian_ref=att-0123456789abcdef
for dataset_kind in training evaluation; do
  codex mcp add "opentour-$dataset_kind-onboarding" -- "$pilot_python" -I -S -B "$pilot_bundle" mcp-onboard \
    --organization "$pilot_org" --store "$pilot_dir/training.sqlite3" --manifest "$pilot_dir/$dataset_kind.json" \
    --authorization "$pilot_dir/$dataset_kind-grant.json" --custodian-attestation "$custodian_ref"
done
```

Reload those connections. On each, call `preview_dataset_onboarding` with `{}` and verify the intended mode, permissions, rows and revision. In the separate custodian shell, set the appropriate `dataset_kind` and copy that connection's exact 64-character revision. Run the grant command once for training and once for evaluation, using the corresponding preview each time. The 30-minute expiry below is for the generated rehearsal.

```sh
dataset_kind=training
approved_revision=PASTE_THE_REVIEWED_REVISION_FOR_THIS_DATASET
expires=$("$pilot_python" -c 'import time; print(int(time.time()) + 1800)')
"$pilot_python" -I -S -B "$pilot_bundle" onboarding-authorize --organization "$pilot_org" \
  --store "$pilot_dir/training.sqlite3" --manifest "$pilot_dir/$dataset_kind.json" \
  --authorization "$pilot_dir/$dataset_kind-grant.json" --custodian-attestation "$custodian_ref" \
  --dataset-revision "$approved_revision" --expires-at "$expires"
```

Preview again and call `confirm_dataset_onboarding` with that revision in the same MCP connection. Confirm the `registered` receipt and matching revision for each dataset. The kit's published expected-training revision belongs to its offline example. Use the revision returned for your actual trainer-bound manifest.

Before starting Slack, check the local catalog and give each actor only the IDs they are authorized to use. A reviewer rehearsing both dispositions needs `generated-intake` **and** `generated-supplied` in their actor's `datasets` list; a training-only operator can be limited to `generated-intake`. If the trainer and presentation reviewer differ, add the real trainer as a separate actor with `inspect` and `tune` for the training dataset. Populate grant expiry and separate preview/aggregate permissions for each person.

The administrator records each readiness check privately before the first real callback. The syntax check can succeed even when no people have active grants.

| Check | Evidence needed to proceed | If missing |
| --- | --- | --- |
| Named reviewer and recipient | The reviewer is in the private channel, has a current review and aggregate grant for the intended dataset, and agrees who may receive the evidence. | Record the missing person, permission or audience. Keep the live rehearsal waiting. |
| Authorized dataset | The catalog contains the agreed dataset ID and revision for that organization. Its mode and limits match the custodian's intake. | Repeat the private MCP preview and resolve the scope with the custodian. |
| Training, when selected | The intended trainer is listed for this exact training dataset and has an unexpired tuning grant. | Correct the private role or obtain a freshly authorized revision. The supplied-output path remains available to eligible reviewers. |
| Worker and restart | The approved operator starts one worker, checks its connection to the selected workspace, and restarts it against the original working directory and current custody ledger. | Hold live signoff until the operator can reproduce startup and identify the support owner. |

```sh
"$pilot_python" -I -S -B "$pilot_bundle" data --db "$pilot_dir/training.sqlite3" --org "$pilot_org" catalog
"$pilot_python" -I -B "$pilot_bundle" slack "$pilot_dir/worker.json" --check-config
"$pilot_python" -I -B "$pilot_bundle" slack "$pilot_dir/worker.json"
```

Run the worker with the locally injected tokens. An optional `--post-launcher` invocation sends a neutral launcher into the approved channel. Use it only after the workspace owner authorizes that post.

Open `/opentour` to find the reviewed, pending or returned evidence available to your actor. Select the relevant readout when several are available. Ask the bounded questions in its private evidence view to understand errors, coverage and limits, paired changes, or the intended audience. Return to the same evidence before choosing its disposition. Open `/opentour search FIELD` to look for authorized data; the search term can contain up to 64 characters.

1. In the approved private channel, enter `/opentour data` or `/opentour search label`. Open a dataset. Verify its approved purpose, revision, visible split sizes and any explicitly permitted training preview. Evaluation-only enrollment displays the frozen supplied comparison. A training-enabled enrollment offers **Tune this dataset** to an authorized trainer.
2. Review the frozen data revision, CPU profile, epochs, maximum updates, CPU limit and intended audience. **Start training** confirms one fit. Record the job ID privately and open `/opentour status JOB`. A working message reports that a lookup is underway. The durable job record supplies the actual execution state. A prompt such as `approve` grants no authority.
3. After completion, the authorized reviewer opens **View evidence**. Check the tested cohort, method revision, audience, errors over observed automatic decisions, coverage, manual review, missing and unknown outputs, paired changes and the ceiling. Return to the workspace and confirm **Approve for readout** for the trained `generated-intake` revision. Repeat the same action and verify the original local receipt. Open the separate `generated-supplied` evaluation-only dataset, inspect its own evidence, return to the workspace and confirm **Request changes** for that revision. A canceled dialog, stale revision or failed view cannot clear a handoff. Presentation approval covers only its named revision and recipient.
4. Run `/opentour` in the second channel, or with a member who lacks the dataset grant. Verify that no protected dataset, example, aggregate or decision appears. This negative check uses a real Slack callback. Keep local tests and actual install results identified separately.
5. With a known receipt, the owner can inspect its local evidence and delivery state. Run `pilot-venv/bin/python -I -B opentour-slack.pyz slack /private/customer-pilot/worker.json --receipt RECEIPT` in the approved directory. Keep the actual receipt private. The workflow owner separately records the recipient's rerun or witnessed repeat and the business decision.

If a training worker's lease expires, check the original job, reviewer, proposal revision, authorization and recoverable state on the owner machine. Recover the same confirmed job. Use the original proposal revision here, which differs from the dataset and presentation revisions.

Recovery scores the same held-out cohort from the verified fitted checkpoint. A missing checkpoint ends the job with `interrupted_fit_unverified`. A changed checkpoint ends it with `checkpoint_unverified`. Record the failure locally. That confirmation authorizes only one fit.

```sh
pilot-venv/bin/python -I -B opentour-slack.pyz data --db /private/customer-pilot/training.sqlite3 --org generated-demo status JOB
pilot-venv/bin/python -I -B opentour-slack.pyz data --db /private/customer-pilot/training.sqlite3 --org generated-demo recover JOB --reviewer UREVIEWER --expected-revision PROPOSAL_REV
```

## Optional owner-operated Zapier receipt

Name the Zap administrator and have the owner authorize the exact destination, recipients, retention, fields and purpose **before any send**. If the owner requires zero external traffic, run the evaluation locally. The Slack and Zapier connections each transmit their approved information to another service.

1. In the customer's Zapier account, the administrator creates and enables **Webhooks by Zapier → Catch Hook**. Inject the complete secret URL privately as `OPENTOUR_ZAPIER_HOOK_URL` into the owner worker. Run `pilot-venv/bin/python -I -B opentour-slack.pyz slack /private/customer-pilot/worker.json --fingerprint-zapier`. Record only the printed SHA-256 in `zapier_destination_sha256`, then explicitly enable `zapier`. Keep the raw URL in the secret store. [Zapier Catch Hook setup](https://help.zapier.com/hc/en-us/articles/8496288690317-Trigger-Zap-workflows-from-webhooks)
2. Configure downstream deduplication by opaque receipt. The default fields are `event` (`pilot_decision_recorded`) and `receipt`. Separately authorize and enable `zapier_disposition` to add `disposition` with `approved_for_readout` or `changes_requested`. The recorded authorization and current policy must permit that field. Save the config, recheck it, and exercise a new generated decision for this destination.
3. Choose the delivery schedule the owner approved. For one manual pass, run `pilot-venv/bin/python -I -B opentour-slack.pyz slack /private/customer-pilot/worker.json --flush-zapier`. For automatic forwarding, start the Socket Mode worker with `pilot-venv/bin/python -I -B opentour-slack.pyz slack /private/customer-pilot/worker.json --auto-flush-zapier`. This opt-in worker checks its durable outbox at startup, after an eligible new decision, and every 30 seconds. Each send rechecks the current engagement, reviewer grant, destination binding and permitted fields. A restart can deliver a still-pending authorized receipt. A changed binding, expired permission or withdrawal holds or suppresses it. Once a send has been admitted, an uncertain result requires owner reconciliation. A missing usable hook holds forwarding while the Slack worker remains available.
4. HTTP success records transport acceptance. The Zap owner finds the exact receipt in the authorized Zap's run history and checks the intended downstream action. Only after verifying it, run `pilot-venv/bin/python -I -B opentour-slack.pyz slack /private/customer-pilot/worker.json --confirm-zapier RECEIPT --downstream-ref OPAQUE_OWNER_TAG`. Reopen the local receipt to verify the recorded state. For an ambiguous send, reconcile the original receipt first. A repeat can duplicate the destination action.

On withdrawal, the custodian starts the engagement closeout on the owner machine. Use `pilot-venv/bin/python -I -B opentour-slack.pyz custody --directory PRIVATE_DIR --org ORG withdraw --help` to see the required operator and terms arguments. Run the documented withdrawal, then follow the kit guide for confirmed, retained and uncertain copies. Record previously delivered Slack/Zapier copies in the private closeout evidence. An HTTP response, Slack button, training completion or downstream delivery alone cannot establish customer acceptance.
