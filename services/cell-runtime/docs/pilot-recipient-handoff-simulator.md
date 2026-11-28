# Reconcile an approved handoff on an isolated generated tenant

`integrations/zapier/handoff-recipe-v1.json` is the disabled handoff recipe.
The hosted trigger carries one opaque `release_ref` per committed worker
outbox row. Repeated triggers retain that reference. The connection supplies
the tenant and acting principal. Keep approval contents, current grants,
artifact paths, archive bytes and recipient details inside their respective
private boundaries. The local simulator exercises the worker selection and
independent receiver custody through a generated installation.

## Local setup and field map

Use an owner-only configuration file in the generated worker's existing
private directory beside `training.sqlite3`. Set `schema_version` to
`opentour.pilot.recipient-handoff-simulator.v1`. Set `tenant` to the generated
organization and `actor` to the explicitly scoped local operator. The
operator needs `inspect`, `claim` and `reconcile` on the current signed
destination permission. Use the exact worker-side `selection` object with
`workflow_ref`, `proposal_ref` and `approval_ref`, and the absolute
`recipient_directory` of the disjoint enrolled receiver. Enrollment puts
`recipient.json` and the authorized local `offered.zip` in that directory.
The sender key, destination permission and signed recipient entitlement
follow [the existing recipient delivery setup](pilot-recipient-delivery.md).

In the private worker directory, write the committed trigger as a private
JSON file with exactly one `release_ref` field. Invoke the same installed
recipe and trigger for each retry. The runner verifies the shipped recipe's
exact bytes and reads only the opaque reference from the trigger. Worker
identity and recipient authorization come from private configuration and
current signed grants.

```sh
uv run --frozen python -m opentour_cell.pilot.recipient_handoff_simulator \
  --configuration "$GENERATED_WORKER/generated-handoff.json" \
  --recipe services/cell-runtime/integrations/zapier/handoff-recipe-v1.json \
  --trigger "$GENERATED_WORKER/committed-release-event.json"
```

Use the bundled recipe from the extracted kit.

```sh
python3.12 -I -S -B opentour-slack.pyz recipient-handoff-simulator \
  --configuration "$GENERATED_WORKER/generated-handoff.json" \
  --recipe integrations/zapier/handoff-recipe-v1.json \
  --trigger "$GENERATED_WORKER/committed-release-event.json"
```

For a direct local check, supply `--release-ref "$COMMITTED_RELEASE_REF"`
in place of the recipe and trigger arguments.

The result carries `schema_version`, `release_ref`, `state`,
`receiver_receipt_ref` and `owner_action`. A `delivery_unconfirmed` result
calls for current independent lookup. The enrolled receiver verifies its own
stored archive, bound artifact digest and recipient signature. An existing
committed claim with unknown or absent destination state remains held for
the authorized operator with zero resend. If no claim ever committed, a
verified lack of receiver state can return the row to `queued`. A later
trigger must obtain verified absence again before claiming. Authority
refusal returns exit 2 with no receipt.

The generated drill adds `--simulate-lost-ack` to exit 75 just after the
receiver commits. Run without that switch after restart. At the destination,
retain the signed receipt and independently compare the exact package digest
before counting delivery. The recipient uses `repair_bundle replay` under
its own grant to verify the frozen candidate and fallback readouts. On an
unverifiable receipt, keep the handoff pending, preserve the original
offer and custody, and recover authorized access before checking again.
Withdraw the sender permission with its custodian marker and the recipient
entitlement through `repair_bundle withdraw-recipient`. Retain the historical
receipt in private custody.

```sh
uv run --frozen pytest -q services/cell-runtime/tests/pilot/test_recipient_handoff_simulator.py
```

The local recipe leaves production activation disabled. A production or
separate test-target change requires its own current operator authority,
compatible pinned rollback target and a destination-issued readback. The
hosted workflow still needs its authenticated fetch and receiver connection;
installed tests and customer replay have separate qualification owners.
