# Reading a private live export

An authorized worker can import one immutable revision of a customer-owned, structured decision or outcome export. The customer supplies local JSON files. The worker reads them and records scoped events and page receipts in its local SQLite store.

## Worker command

When the installation contains `readiness-selection.json` for the admitted workflow, the worker refreshes
durable readiness after the source operation is materialized. That selection
references an independently authorized worker with both live inspect and intake
rights. It grants no rights itself. Missing repair inputs remain explicit holds.
If readiness refresh fails after source commit, the command fails without claiming
readiness success; retry reconciles readiness without rereading acknowledged pages.
An unavailable matching readiness receipt is a hold; a selection for a different
workflow does not activate this optional step. Slack status reads never invoke this refresh.

Use the existing private worker directory, current signed live authority, current engagement and previously admitted workflow. Its custodian saves a decision or independent-outcome binding through **Connect live decisions**. The selected worker actor needs the intake role. Each currently bound resource needs an entry in the signed export mapping. A decision-only workflow can record pending decisions before an outcome binding exists.

The private selection JSON has exactly the following fields.

```json
{"schema_version":"opentour.pilot.local-live-selection.v1","tenant":"generated-org","workflow_ref":"generated-workflow","actor":"worker"}
```

Use the normal MCP `submit_intake` tool with `mode=live`, the opaque `feed_binding_ref`, its current `source_revision` and an `idempotency_key`. On the worker host, pass the resulting run reference and the absolute path of that selection to the installed Python module.

```sh
python -m opentour_cell.pilot.live_reader "$private_selection" "$admitted_run_ref"
```

Exit 0 prints `materialized` after committing the worker receipt or `already_materialized` for a completed, currently authorized run. Exit 1 prints `live_export_unavailable`. Exit 2 reports the expected arguments. A changed or unavailable grant, binding, selection, file or digest can hold the run. Repair access to the same authorized revision and rerun the same command to resume.

## Export manifest

Place the files in the worker-owned private selection directory. The directory must exclude group and other access. Each input must be a private, singly linked regular file owned by the worker. The reader opens each input without following its final symlink. It validates the signed mapping before opening catalogs, validates catalog metadata before reading pages, and rechecks authority before applying a page. The mapping limit is 32 KiB, a catalog is at most 64 KiB, and a page is at most 256 KiB and 1,000 rows. A catalog has 1–32 pages. Catalog and page filenames use the prefixes in this example followed by 1–96 ASCII letters, digits, underscores or hyphens and the suffix `.json`.

The following generated example constructs all three documents for one deferred decision. The variables `signed_body` and `signing_key` refer to the existing custodian-authorized local grant body and key bytes. Signing belongs to that authority. Never pass the key to a chat client or publish private manifests. Select a **current decision grant** in the example. A page can cover a stream with zero rows.

```python
import hashlib
import hmac
import json

def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()

def pin(filename, value):
    return {"file": filename, "sha256": digest(value)}

grant = next(g for g in signed_body["grants"] if g["kind"] == "decision")
scope = {k: signed_body[k] for k in
         ("tenant", "workflow_ref", "engagement_epoch", "dataset_ref", "dataset_revision")}
event = {
    "kind": "DecisionRecorded", "event_id": "generated-delivery", "event_revision": 1,
    "decision_id": "generated-D1", "decision_revision": 1, "case_id": "generated-case",
    "family_id": "generated-family", "decision_at": 90, "action_ref": "defer",
    "policy_ref": "generated-policy", "input_ref": None, "input_fidelity": "unavailable",
    "source_provenance": "generated-application-commit", "actor_ref": None, "model_ref": None,
}
position = grant["checkpoint"] + 1
epoch = scope["engagement_epoch"]
page_ref = "generated-page-1"
metadata = {"interval_start": 50, "interval_end": 100, "coverage_through": 100,
            "complete": True, "next_cursor": None}
page = {"schema_version": "opentour.pilot.live-export-page.v1", "page_ref": page_ref,
        "rows": [{"position": position, "epoch": epoch, "event": event}]}
page_file = "live-export-page-generated-1.json"
descriptor = {
    "page_ref": page_ref, "checkpoint": position, "pin": pin(page_file, page),
    "read_metadata": metadata,
    "page_digest": digest([grant["source_revision"], [event], [position], [epoch],
                           position, metadata]),
}
catalog = {"schema_version": "opentour.pilot.live-export-catalog.v1", "scope": scope,
           "grant": grant, "event_revisions": "versioned", "pages": [descriptor]}
catalog_file = "live-export-catalog-generated-decisions.json"
mapping = {"schema_version": "opentour.pilot.live-export-mapping.v1",
           "authority_revision": digest(signed_body),
           "resources": {grant["resource"]: pin(catalog_file, catalog)}}
envelope = {"mapping": mapping,
            "seal_sha256": hmac.digest(signing_key, encoded(mapping), "sha256").hex()}
mapping_file = "live-export-" + scope["workflow_ref"] + ".json"
```

Write `page`, `catalog` and `envelope` to their respective filenames. Each digest covers the canonical JSON value. Include entries for additional bound resources before signing the mapping. The grant object is the exact signed projection with `resource`, `kind`, `revision`, `source_revision`, `deciding_namespace`, `checkpoint`, `lease_ref` and `verifier`. The worker pins the catalog digest to the binding and source revision before committing its first page. Changes to a source export require a new authorized source revision and admission.

## Row and page semantics

| Input | Worker interpretation |
| --- | --- |
| Row position and epoch | The trusted export attests the source position and engagement epoch. A row at or below the activation checkpoint or carrying another epoch enters scoped quarantine. |
| Delivery and decision identity | `event_id` and `event_revision` identify a resource delivery. `decision_id` and `decision_revision` identify the application commit within the authorized deciding namespace. Case and family group decisions. Only a catalog explicitly marked `immutable_event_id` may omit the envelope revision. The normalized value is 1, including in `page_digest`. |
| Decision, time and input | `decision_at` is the application commit time. An explicit `defer` or `ask` is a committed action. Use `observed`, `reconstructed` or `unavailable` for original-input fidelity. Unavailable input has a null reference. Keep raw request and response content out of this format. |
| Independent outcome | Use the separately granted outcome resource, authorized verifier and matching deciding namespace. An outcome event carries exactly `kind="OutcomeVerified"`, `event_id`, `event_revision`, `decision_id`, `decision_revision`, `outcome_id`, `outcome_revision`, `effective_at`, `recorded_at`, `finality`, `verified_action_ref` and `verifier`. A final outcome supplies the verified action reference. A reopened or withdrawn revision carries null. The worker records its ingestion time separately. |
| Accepted and excluded evidence | Repeated deliveries count once. A later independent final can verify the generated pending decision. A higher reopened outcome revision clears current verification. A provider attempt or an event containing an extra raw field such as `raw_reply="GENERATED_PRIVATE_SENTINEL"` yields zero valid evidence. Conflicting or unattributable evidence stays disputed or quarantined. |

Use integer UTC epoch seconds for event times and interval endpoints. An empty page has an empty row list and corresponding empty event, position and epoch lists in its digest. A completed empty poll records a zero-row receipt. Unknown queried intervals have both endpoints null. Unknown coverage has `coverage_through=null`. The worker records its own poll time. The last page has a null cursor. Intermediate pages set `complete=false` and name the next page reference in `next_cursor`.

The worker atomically commits each page's normalized events, checkpoint and read receipt. If page two is unavailable, page one's receipt remains available and the run stays unfinished. A retry validates the original signed catalog and resumes at the first unacknowledged page. Consumers can use the completion flag and coverage fields to distinguish partial progress from a completed, fresh poll. This adapter operates on one bounded resource per admitted run. Its generated tests exercise local software behavior. Customer source qualification and paired cross-resource refresh require their own evidence.

From the installed Slack workspace kit, run
`python opentour-slack.pyz live-reader <private-selection> <admitted-live-run>`.
After successful ingestion the worker refreshes configured readiness. On restart
Home reads the persisted worker verdict and the scoped page receipt.
