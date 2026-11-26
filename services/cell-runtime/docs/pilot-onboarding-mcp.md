# Register a dataset from a local MCP client

Use Python 3.12.16 and the packaged `opentour-slack.pyz` on the customer's approved workstation. Each MCP connection selects one private manifest, organization, store and custodian attestation. Point the customer's Slack app at that same organization and `training.sqlite3`. Slack workspace and dataset access grants are configured separately.

## Choose a permitted dataset

| Enrollment | What the customer provides | What the dataset permits |
| --- | --- | --- |
| Evaluation only | Frozen incumbent and repair decisions, independent held-out references, visible fields and an agreed error ceiling | Inspect metadata and score those supplied decisions. The training row count is zero. |
| Local text classification | Separately authorized training rows, held-out decisions, an incumbent, reviewer scope, approved fields and an error ceiling | Inspect metadata, score decisions and propose a CPU text-training job. Row previews require explicitly projected training examples and a separate viewer grant. Starting training requires its own approval. |

For a synthetic rehearsal, select the applicable generator. Both print a complete manifest. Save the generated JSON inside an operator-owned directory with mode `0700` and set the file mode to `0600`.

```sh
python3.12 -I -S -B /opt/opentour/opentour-slack.pyz data --org generated-demo --db /private/generated-demo/training.sqlite3 generated-evaluation-fixture
python3.12 -I -S -B /opt/opentour/opentour-slack.pyz data --org generated-demo --db /private/generated-demo/training.sqlite3 generated-fixture --reviewer generated-reviewer
```

For customer data, the custodian selects the allowed destination and records retention in the manifest before approving its revision. Use `owner_local_store` or an approved opaque `dest-` reference followed by 16–64 lowercase hexadecimal characters. For retention, provide a disposition of `delete`, `return` or `retain` and an ISO date, or an opaque private terms reference. For example, use `{"disposition":"return","date":"2030-12-31"}` or `{"terms_ref":"terms-0123456789abcdef"}`. A legacy training manifest with unrecorded retention can be previewed. A new enrollment requires agreed terms and an authorization for the resulting revision.

For a decision against the customer's limits, the workflow owner declares minimum automatic coverage and maximum wrong-automatic rate against the frozen source before inspecting its answers. Bind the agreement to the enrollment manifest before issuing the custodian grant. The owner can reproduce the original descriptive comparison without an agreement. [Operating agreement, worked example and replacement procedure](pilot-operating-point.md)

## Connect the client

In the private intake register, the operator records the actual custodian, permission, approved destination and retention. Assign that attestation an opaque reference of the form `att-` followed by 16–64 lowercase hexadecimal characters. Use the reference when launching the MCP server and issuing the separate grant. Only trusted operators may manage consent in this directory. Configure the MCP client with access to the two onboarding tools and reserve the operator shell and grant-file writes for the custodian or their delegate.

Replace the sample reference and the interpreter, bundle and private paths below. Run this in the Codex CLI and reload its MCP connections.

```sh
codex mcp add opentour-onboarding -- /opt/python3.12/bin/python3.12 -I -S -B /opt/opentour/opentour-slack.pyz mcp-onboard --organization org_01 --store /private/org_01/training.sqlite3 --manifest /private/org_01/dataset.json --authorization /private/org_01/onboarding-authorization.json --custodian-attestation att-0123456789abcdef
```

Call `preview_dataset_onboarding` with `{}`. The response contains the immutable SHA-256 dataset revision, permitted purpose, field names and actions, destination classification or opaque reference, retention, and training and held-out row counts. It also reports the current registration, authorization, and the UTC Unix expiry of a valid grant. The grant expiry controls enrollment authority. The retention entry records the separate terms for the data. Case contents, identities, the custodian attestation, local paths and credentials stay private.

The first preview normally reports `absent` and `authorization_required`. It leaves a missing database untouched. A matching catalog revision reports `registered`. An occupied dataset identifier with another revision reports `stale`. A withdrawn organization, revoked or expired dataset, or inaccessible store returns only `{"registration":"unavailable"}`, including when an old training database is restored beside the private closeout record. A changed on-disk manifest requires restarting the connection and previewing the replacement.

## Authorize the selected revision

The custodian checks the exact preview against the permitted organization, purpose, fields, actions, destination and retention in the private register. From a separately controlled operator terminal, supply the matching attestation, the previewed revision and a future UTC expiry in Unix seconds.

```sh
/opt/python3.12/bin/python3.12 -I -S -B /opt/opentour/opentour-slack.pyz onboarding-authorize \
  --organization org_01 --store /private/org_01/training.sqlite3 \
  --manifest /private/org_01/dataset.json --authorization /private/org_01/onboarding-authorization.json \
  --custodian-attestation att-0123456789abcdef \
  --dataset-revision "$APPROVED_REVISION" --expires-at "$EXPIRY_UNIX_SECONDS"
```

The helper writes a private version-2 permission binding the exact dataset, organization, store, attestation, scope, disposition and expiry. It preserves an existing grant file. To replace an older or withdrawn permission, the authorized operator explicitly removes it and issues a new grant against the reviewed data. The operator's private record establishes who approved the use.

## Confirm and verify

Call `confirm_dataset_onboarding` with `{"dataset_revision":"<64 hexadecimal characters from preview>"}` in the same MCP session. The server checks the selected manifest and the grant again inside the store's writer transaction. It returns `registered` and the revision. Concurrent and repeated successful confirmations produce one catalog entry. After reconnecting, preview again before confirming. A changed grant expiry requires a fresh preview. Missing or expired permission, a mismatched attestation or scope, a changed revision or a withdrawn enrollment blocks confirmation.

A Slack member with an active grant for that dataset can run `/opentour data` in the approved private channel and open the dataset. Check that the revision in its details equals the MCP receipt. An evaluation-only entry shows zero training cases and supports the frozen comparison. A member granted other datasets can see only their own catalog. The custodian can also inspect the shared store directly.

```sh
/opt/python3.12/bin/python3.12 -I -S -B /opt/opentour/opentour-slack.pyz data --org org_01 --db /private/org_01/training.sqlite3 catalog
```

To withdraw enrollment authority, remove the private grant. To withdraw subsequent access to an already registered dataset, also run `data --org org_01 --db /private/org_01/training.sqlite3 revoke <dataset-id>`. Follow the recorded return, deletion or retention terms for existing local and authorized external copies.

[Codex MCP client configuration](https://developers.openai.com/codex/mcp/) · [Read-only offline evaluation MCP](pilot-mcp.md)
