# Run the dedicated recipient handoff connection

Use an owner-operated worker with its existing private training store, current
handoff permissions and an enrolled recipient custody directory. The worker
and recipient custody use separate private directories on the same managed
host. They share the final release and custody ordering needed to deny a
withdrawn claim before installation. The recipient fetches the encrypted
bundle over HTTPS with its own TLS client key. This installation supports a
co-located recipient. A remote-only custody service would need its own
coordinated commit boundary.

## Install privately

The custodian seals the connection document using the current local handoff
permission key. Use the same HMAC-SHA-256 envelope format as the existing
handoff permission. The seal covers the canonical JSON object in `grant`.
Place the envelope in the worker's private directory. Keep all configuration,
certificate, key and package files under the respective owner's private custody.

| Connection grant field | Owner-controlled value |
| --- | --- |
| `schema_version` | `opentour.pilot.handoff-connection.v1` |
| `tenant`, `engagement` | Exact current worker and engagement |
| `origin` | Dedicated canonical HTTPS origin without a path |
| `audience` | Opaque audience for the dedicated dispatcher connection |
| `dispatcher_actor` | Existing actor authorized to inspect, claim and reconcile the release |
| `dispatcher_token_sha256` | SHA-256 of the private dispatcher bearer. Use 43–128 URL-safe characters. |
| `recipient`, `destination_ref` | Current recipient and destination in the signed handoff permission |
| `recipient_tls_sha256` | SHA-256 of the separate recipient client certificate in DER form |
| `recipient_signer_sha256` | SHA-256 of the enrolled custody certificate's PEM bytes |
| `connection_ref`, `expires_at` | Opaque connection revision and epoch-second deadline |

Enroll the approved recipient entitlement with the existing bundle command.
Place each exact encrypted export in the worker's directory as
`handoff-package-<release_ref>.zip`, with owner-only permissions. The endpoint
verifies its original entitlement, destination, encryption certificate and
package digest. The encrypted assets in the ZIP transfer over TLS. The
recipient's existing custody certificate signs receipts. Its separate
TLS client certificate authenticates the fetch and readback connection.

Use private TLS files and the exact origin from the sealed connection.
The server accepts a dispatcher authenticated by the configured bearer and
audience and verifies the recipient certificate separately on private routes.
Keep the existing intake listener and its credentials under their original
installation.

```sh
python -I -S -B opentour-slack.pyz recipient-handoff-https \
  --connection "$HANDOFF_CONNECTION" --recipient-grant "$RECIPIENT_CUSTODY" \
  --host "$HANDOFF_BIND_HOST" --port "$HANDOFF_PORT" \
  --server-cert "$HANDOFF_SERVER_CERT" --server-key "$HANDOFF_SERVER_KEY" \
  --recipient-ca "$RECIPIENT_AUTH_CA" --recipient-cert "$RECIPIENT_AUTH_CERT" \
  --recipient-key "$RECIPIENT_AUTH_KEY" --server-ca "$HANDOFF_SERVER_CA"
```

## Connect the disabled workflow

Use the [v2 Zap field map](integrations/zapier/handoff-recipe-v2.json). Map only
the committed outbox's `release_ref` from Catch Hook to a fixed request to
`POST /v1/handoff/dispatch`. Keep the hook URL private. Store the dispatcher
bearer and audience in the dedicated API by Zapier connection and restrict
its domain to the approved HTTPS hostname. Build the workflow with the Zap
off. The field map records the steps for manual assembly. Provider installation
and activation require separate authorization.

The approval producer supplies an already committed outbox reference. The
listener has no remote reservation operation and no hook URL configuration.
Bind and verify the producer separately before using an installed Zap.

The dispatcher returns an opaque reference, a state and any owner action.
The recipient's authenticated fetch uses a one-use ticket scoped to the
current claim, exact encrypted package and earliest deadline. The recipient
commits one signed custody record under the existing worker and recipient
guards. The worker records delivery after checking both a fresh signed
observation for its challenge and the original custody receipt.

## Recover or revoke

Retry the same opaque release reference. A claimed delivery with a missing
or unverifiable receipt stays `delivery_unconfirmed` for the authorized
operator. Preserve the original custody and outbox records. Restore the
approved private recipient connection and reconcile the existing release
before making any new proposal. A later compatible proposal requires its
own fresh inspection, decision and outbox entry.

Remove old bearer material from the API connection after rotation. Re-seal
the owner-local grant with the new credential fingerprint or enrolled
recipient client certificate and restart if the TLS trust roots or origin
change. The worker rechecks connection and grant revisions at fetch and
custody publication. Revoke the connection in the private worker with the
owner-local command. It serializes the dedicated connection revocation with
the final custody commit and retains the existing approval and receiver
records.

```sh
python -I -S -B opentour-slack.pyz recipient-handoff-https \
  --connection "$HANDOFF_CONNECTION" --revoke
```

Use the existing custody withdrawal command when recipient use must stop.
Record previous disclosures through the existing custody process.

For recipient replay and compatible fallback, use the separate recipient
and test-target authority documented in
[the recipient handoff runbook](pilot-recipient-handoff-simulator.md).
Production activation requires its own approved change authority.

## Verify with generated data

```sh
just test-python services/cell-runtime/tests/pilot/recipient_handoff_tls_fixture.py
just test-python services/cell-runtime/tests/pilot/test_handoff_recipe_v2.py
```

The finite loopback target uses generated worker permissions, release approvals,
TLS keys and recipient custody. For each test tenant, delivery is initially
unconfirmed after a claimed fetch. A fresh signed readback after a lost reply
confirms the same digest and original receipt. Repeating the same dispatch
keeps one outbox row and one recipient installation. Foreign scope, invalid
receipts and expired or withdrawn authority fail closed. Record the pinned
source and loopback cleanup receipt when qualifying the local package.
