# Emit committed invoice decisions

The [invoice application example](../examples/invoice_decision_emitter.py) uses a
customer-local SQLite journal. It keeps provider attempts separate from invoice
decisions. Call `attempt` for each provider attempt and `commit` when the invoice
application commits its effective action. In this example the commit is the
application's authoritative record and its outbox event is written in the same
transaction. An exact retry returns the original event reference and source position.

## Run the generated example

Use the repository's CPU Python environment and create the scope file in a private
directory owned by the local user. Its tenant, workflow, engagement, deciding
namespace, policy and baseline position must match the authorized source. The example
values below are for generated data. Use opaque application references for the
operation, attempt, decision, case and family.

```sh
example_dir="$(mktemp -d)"
scope="$example_dir/scope.json"
(umask 077; cat > "$scope" <<'JSON'
{
  "schema_version": "opentour.example.invoice-decision-emitter.v1",
  "tenant": "org-a",
  "workflow": "generated-invoice-flow",
  "engagement_epoch": "generated-epoch",
  "namespace": "invoice-app",
  "policy_ref": "invoice-policy-v1",
  "checkpoint": 10
}
JSON
)
example=services/cell-runtime/examples/invoice_decision_emitter.py
python "$example" --scope "$scope" initialize
python "$example" --scope "$scope" attempt --operation operation-D1 --attempt attempt-1
python "$example" --scope "$scope" attempt --operation operation-D1 --attempt attempt-2
python "$example" --scope "$scope" commit --operation operation-D1 --attempt attempt-2 \
  --decision D1 --case invoice-1 --family supplier-1 --action freight --decision-at 100
python "$example" --scope "$scope" export --page-ref page-a --after 10 --through 11
```

The export receipt has an opaque file name, canonical page digest, committed row
count and last source position. Only decision metadata is in the private page. The
example accepts committed actions `freight`, `review`, `defer`, `ask` and
`supplies`. The last action lets the generated P1 journey capture the
application's original supplies decision before a verifier corrects it to freight.
The example does not accept invoice content or a provider response.

## Optional independently observed input metadata

Content capture stays off by default. `commit --observation observation-D1.json`
can consume an independently signed application/model observation after the owner
explicitly installs `invoice-observation-selection.json` in the private application
directory. It never accepts raw inputs, a caller's model assertion, a private key
or a receipt path outside that directory. An ordinary commit still records
unavailable input fidelity and no model attribution, even when a selection exists.

The selection schema `opentour.example.invoice-observation-selection.v1` contains
`grant_ref`, the exact scope's `tenant`, `workflow`, `engagement_epoch`, `namespace`
and `policy_ref`, authorized `producer_ref`, `model_ref`, immutable `model_revision`,
`public_key_file`, `public_key_sha256` and integer `expires_at`. These are an explicit
owner-installed observation permission; the example does not create this file.
The public key's basename and SHA-256 are pinned. Removing/changing the selection,
expiry or `invoice-observation-withdrawn-<grant_ref>.json` fences further observed
commits. Existing committed metadata remains historical application evidence and
requires the separate current intake grant for disclosure to the worker.

The private receipt is `{claim, signature}`. The independent producer signs the
canonical JSON claim with the existing bundle SHA-256 signature contract; signature
bytes are base64. The claim's schema is `opentour.example.invoice-observation.v1`
and its fields are `grant_ref`, `producer_ref`, the five scope fields above,
`operation`, `attempt`, `decision`, `case`, `family`, `decision_at`, `action`,
`model_ref`, `model_revision`, opaque `input_ref` and opaque `receipt_ref`.
Unknown fields, including input content and later outcomes, are rejected. Producer
signing keys stay outside the application example and custodian intake keys are
never supplied to it.

Verification binds the actual application commit arguments, selection and exact
producer/model identity. Inside the durable transaction the emitter checks the
selection, public key, expiry/withdrawal marker and immutable receipt before and
after the commit or retry. The event retains only opaque input, producer and model
references plus observed fidelity. Its provenance digest binds the full signed
receipt, exact model revision and selection. Keep the original private receipt
for independent audit. No model/output correctness or customer accuracy follows
from successful signature verification.

An observed retry uses the same receipt and its exact winning attempt. A different
attempt requires its own independent receipt and cannot rewrite the earlier event.
The existing default commit behavior still permits two attempted deliveries of one
operation to recover its original authoritative commit.

Acceptance map for this OPE-515 evidence-only extension:

| Clause | Positive witness | Adverse witness |
| --- | --- | --- |
| 515 C / K5 observed boundary | Independent generated signature, exact opaque input and model, same durable event after restart | Caller assertion, wrong producer/model/scope/operation/attempt/action/time and raw fields deny |
| 541 default-off content | Ordinary attempt/commit/export unchanged, no observation reads | Receipt with no current opt-in, expired/revoked grant or unsafe path denies |
| Current authority at effect | Receipt/key/selection rechecked inside transaction | Mutation/withdrawal at DB admission and after INSERT rolls back; no committed row |
| Retry and disclosure | Same observed receipt reuses event/position; export holds only opaque metadata | Re-signed changed receipt conflicts; no private keys, input sentinel or diagnostics emitted |

These consumer tests use generated signatures only. The OPE-515 journey separately
owns an actual registered CPU producer, input-context binding and independent live
correction/readiness recount on the same workflow.

## Check the connection

The custodian registers the private page in the worker's signed export catalog,
with the exact current grant, resource, source revision and page digest. The
application keeps the journal and exports separate from the custodian's signing
material. A new source revision covering the same engagement may repeat a committed
event from an earlier revision. The worker recognizes the same delivery, so retain
the original event ID and revision.

From private Slack workflow settings choose **Connect live decisions**, select
the authorized decision resource, and save. Reopen Settings to recover the same
opaque binding reference after a retry. An outcome feed and a release reviewer
can be configured separately. The worker's local evaluation identity can call
`discover_sources` to read the current binding, then call the SDK's
`submit_intake` with its exact binding and revision.

```json
{
  "mode": "live",
  "feed_binding_ref": "feed-A",
  "source_revision": "feed-rev-1",
  "idempotency_key": "live-01"
}
```

A queued response is an admission receipt. Run the worker's live-reader command
for its `run_ref` and use `get_run` to check the persisted receipt. The first
committed decision is pending independent verification. When a separately
authorized outcome feed is available, submit that feed with its own binding and
revision. A superseded or unavailable binding needs a fresh authorized discovery
before retry.

The [generated conformance witness](../tests/pilot/test_invoice_decision_emitter.py)
runs the example, the authenticated Slack handler, the official MCP Python SDK
and the worker reader. It checks that two attempts for D1 yield one application
commit, a same-family D2 yields a second decision, and independent truth verifies
only D1. It also checks source revision changes, duplicate admission, frozen
benchmark preservation and zero fit or release effects.

Run the connection witness in a local Python environment with the repository
runtime, pytest and official MCP Python SDK 1.30.0 installed.

```sh
PYTHONPATH=services/cell-runtime/src:services/cell-runtime/tests/pilot python - <<'PY'
import json
import tempfile
from pathlib import Path

from pytest import MonkeyPatch
from test_invoice_decision_emitter import invoice_connection_probe

with tempfile.TemporaryDirectory() as work, MonkeyPatch.context() as patch:
    print(json.dumps(invoice_connection_probe(Path(work), patch), sort_keys=True))
PY
```
