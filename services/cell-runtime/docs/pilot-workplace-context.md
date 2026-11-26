# Read workplace decision evidence through MCP

A customer custodian grants access to one workflow, two bounded resource namespaces and a set of decision-time fields. OpenTour searches the installed reader’s catalog for a matching decision log and an independent correction ledger. The custodian supplies the business scope once. Each refresh discovers the exact resource URIs and versions from the catalog.

## What the custodian supplies

Use an active pilot engagement and its private operator directory. Keep the directory at mode 0700 and the connection file at mode 0600. For first-release live-reader qualification, use an owner-approved Linux x86-64 host with Python 3.12.16, SQLite and descriptor-backed execution through `/dev/fd`. The worker checks the configured absolute executable path and SHA-256 and launches the verified open file. [macOS native-reader compatibility](https://github.com/kevon-dot/opentour-ai/issues/94) remains a separate follow-up.

Install a trusted read-only MCP server or an adapter for the workplace that exposes the resource contract below. Set its credential in a narrowly scoped secret connection. A pinned launcher may obtain credentials or run the vendor process using approved local configuration. A launcher that wraps another executable must use `exec` so the supervised PID is the actual reader. A provider that starts descendants must supervise and reap them. OpenTour bounds reads, closes the reader’s stdin, then terminates and reaps that PID if it ignores shutdown. The launcher is trusted to honor the granted tenant and scopes. Its environment receives the explicitly named `OPENTOUR_WORKPLACE_*` variables. Keep authentication values in the worker’s credential store.

Set these properties in `workplace-connection.json`.

| Property | Required value |
| --- | --- |
| `schema_version` | `opentour.pilot.workplace-connection.v1` |
| `reader` | The MCP server’s exact `provider` name, the absolute `executable` path, the launcher's lowercase `sha256`, and an array `env_names` naming any approved credential variables |
| `purposes` | The two strings `decision_context` and `independent_verification`, in that order |
| `destination`, `data_location` | `owner_local` for both. Snapshot and job data stay in the enrolled owner's directory |
| `scope` | `organization`, `engagement`, `dataset_id`, `workflow`, `field`, `policy_version`, `source_schema_version`, `owner_id`, `owner_attestation`, `adjudicators`, `permitted_features`, `permitted_slices`, `evaluation_allowed`, `as_of`, `expires_at`, `split`, `retention_terms` |
| `roles.trace` | An approved `namespace` ending in `/`, the issuing system's `authority`, a `columns` map, and a `feature_columns` map |
| `roles.correction` | A separate `namespace`, an independent `authority`, and a `columns` map |
| `watermark_mode` | Optional. Use `provider_verified` for a continuing feed. Each resource version must disclose the time through which its records have been verified. The earlier of those two cutoffs governs this snapshot |
| `roles.trace.policy_context` | Optional. Give the approved SOP feature in `feature` and the source timestamp column in `effective_at_column`. Provide the row’s policy version and decision-time availability. Intake excludes later policy evidence |

The scope fields use the same validation as the standard [pilot source intake](pilot-source-intake.md). The custodian sets the adjudicators, evaluation rights, expiry and retention. The discovered pair supplies the source identities and hashes. Each namespace must include a tenant-specific path and a named collection. A protocol URI such as `workplace://billing/tenant-a/event-views/` or a fixed HTTPS collection is an eligible identifier. The pinned reader handles transport for each selected URI.

Map these target keys to exact source column names. All source column names within a role must be unique. The provider exposes a projection containing just these columns.

| Target | Mapping |
| --- | --- |
| Both roles | `event_id`, `source`, `organization`, `workflow`, `field`, `policy_version`, `source_schema_version` |
| Trace | `customer_id`, `group_id`, `decision_at`, `features_available_at`, `incumbent`, and optional `slices` |
| Trace features | Each key in `scope.permitted_features` maps to one explicitly approved decision-time source column under `feature_columns` |
| Correction | `outcome_id`, `adjudicator_id`, `facts_as_of`, `outcome_at`, `verified_at`, `reference` |

For example, a generated billing connection can use `"feature_columns": {"description": "decision_memo"}` and `"columns": {"event_id": "invoice_key", ...}`. The joined reference is supplied by a separately permissioned ledger. Intake preserves review and missing incumbent decisions. Check approved text fields for customer-sensitive content when signing the field map. The reader enforces the agreed projection and rejects known sensitive or outcome-bearing feature names.

## Reader contract

The OpenTour client uses the MCP stdio transport with a negotiated protocol version, `initialize`, `notifications/initialized`, paginated `resources/list`, and `resources/read`. Configure the underlying provider's resource listing to the approved collections. The client disregards listings outside those collections. If a vendor exposes only tools or unrestricted documents, its owner-installed adapter must provide read-only projected resources for this contract. The custodian grants fitting and Slack publication separately. [MCP resources](https://modelcontextprotocol.io/specification/2025-11-25/server/resources) · [MCP stdio](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)

Each catalog entry contains a stable exact `uri`, `name`, `mimeType: application/json`, and a structured `_meta["ai.opentour/workplace"]` object. Populate the object with:

| Metadata | Meaning |
| --- | --- |
| `schema_version` | `opentour.pilot.workplace-projection.v1` |
| `provider`, `authority` | The trusted MCP server name and the configured issuing authority for this role |
| `role`, `source_id`, `version` | Either `trace` or `correction`, a stable issuing source ID, and an immutable provider version |
| `organization`, `workflow`, `field`, `policy_version`, `source_schema_version` | The agreed tenant, workflow, field and versions, exactly matching the custodian scope |
| `policy_status` | `published` |
| `covered_through` | Required for `provider_verified`. Use integer Unix seconds at or after the initial `scope.as_of`, within the active grant and at or before acquisition. Advance the source version when advancing this cutoff |
| `columns`, `row_count` | Sorted list of the exact projected source columns and the integer number of rows this resource contains |

A resource read returns one MCP text content item with the exact selected URI and JSON MIME type. Its text is a JSON object with two keys. `projection` echoes the catalog metadata above. `rows` is an array of projected row objects. Supply every named column and no additional ones. Use integer Unix seconds for timestamps. Each original incumbent decision has either `{"status":"automatic","label":"<decision>"}`, `{"status":"review"}` or `{"status":"missing"}`. The verified reference can be null when the business record is unresolved. Source intake keeps counts of excluded and ambiguous records.

The client inspects up to eight catalog pages and 256 resource descriptors in one catalog pass. It enforces a maximum of 5,000 rows per resource, a 2 MiB resource text limit, a 4 MiB JSON-RPC response limit and a 20-second session limit. A smaller standing `--max-total-rows` is enforced against the declared pair before reading either resource. The client verifies the row counts again on receipt. For `ambiguous_resource_pair`, the custodian can narrow the namespace, published policy or schema and renew the connection approval.

## Discover, refresh and freeze

From the extracted pilot kit, using the private directory and organization for the enrolled customer:

```sh
python3 opentour-slack.pyz workplace-context discover \
  --directory "$PILOT_DIR" --org "$PILOT_ORG" --grant "$PILOT_DIR/workplace-connection.json"
python3 opentour-slack.pyz workplace-context acquire \
  --directory "$PILOT_DIR" --org "$PILOT_ORG" --grant "$PILOT_DIR/workplace-connection.json" \
  --expected-connection-revision "$CONNECTION_REVISION" \
  --expected-pair-revision "$PAIR_REVISION" --max-total-rows 2000
```

Discovery returns the opaque pair and connection revisions, source IDs, versions and row counts. It reads zero resource bodies. Acquisition rechecks the scoped catalog, fetches the chosen pair, checks the catalog again, and validates the underlying source grant and engagement before publishing. The CLI accepts the enrolled connection file and each read uses the specific URI selected from its catalog. A newer catalog version requires discovery again when the caller pins the old choice.

The acquire receipt includes private `trace_path`, `correction_path`, `grant_path`, `receipt_path`, training-independent `snapshot_revision`, aggregate counts and the source proposal. The immutable receipt records both exact resource references, selected fields, provider and normalized hashes, observed fetch times and counts. Feed the first three paths to the existing source-intake freeze command. The original owner export command remains available. Treat this receipt as operator-only.

Repeated pulls of the same source versions and content return the same paired snapshot. An interrupted or rejected refresh leaves the prior files intact. New source versions produce a new revision. A provider that changes a source’s contents while reusing its version receives `workplace_version_content_changed`. Each published pair lives in `workplace-snapshots/<snapshot SHA-256>/`. Include this directory, its version records and the generated `workplace-source-*.json` grants in the owner’s retention and withdrawal inventory.

## Optional training and withdrawal

Base acquisition authorizes evaluation. To authorize fitting against an exact snapshot, the custodian separately signs a private `opentour.pilot.workplace-training.v1` record containing `organization`, `engagement`, `owner_id`, `owner_attestation`, `snapshot_revision`, a distinct `training_attestation` and `training_expires_at` within the base grant’s expiry. Supply its private path with `--training-confirmation` on acquisition. It produces a separate input grant for the same source snapshot. Training requires the existing verified development error class and compute authority as well. A changed snapshot requires a fresh training approval.

Revoke the owner attestation through the existing source-intake withdraw action to block subsequent access, or revoke only the training attestation to withdraw fitting rights. Engagement closeout stops acquisition and the managed workflow. Follow the custodian's retention terms for copies and provider backups.

The software tests use generated data in another process. A live customer pilot also requires a named operator, authenticated reader, custodian-approved tenant, deployed field map and a live scoped receipt.
