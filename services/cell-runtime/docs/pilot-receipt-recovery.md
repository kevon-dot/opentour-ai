# Recover unresolved Zapier receipts on the owner machine

Use the worker's existing private configuration to find deliveries that require operator attention. The output stays inside the owner's terminal. This command reads local SQLite records and runs with the Python standard library, including while Slack is offline.

The standard worker uses SQLite's default journal mode. A closed, intact store in that mode can be inspected offline. If the owner selected SQLite WAL mode, a clean shutdown can remove SQLite's WAL and shared-memory sidecars. Opening such a database in SQLite read-only mode can recreate them, so the discovery command holds the read and leaves the directory unchanged.

For a live engagement, the custodian first checks the current withdrawal and retention record. When the administrator authorizes reconnecting the original worker, keep Zapier automatic handoff disabled, leave its hook out of the worker environment and start the original pinned worker against the same private engagement. Re-run discovery while the worker has the WAL database open with its own sidecars. Recheck current custody, destination consent and any uncertain prior attempt before enabling outbound delivery. If reconnecting is outside the agreement, the sidecars remain absent, or the database and current custody record cannot be verified together, use the custodian's approved backup-review procedure and keep delivery held. SQLite manages its working files.

```sh
python -I -S -B opentour-slack.pyz slack /private/customer-pilot/worker.json --unresolved-receipts --limit 25
```

Each entry has the original opaque `receipt`, its recorded delivery `state`, the original Unix `attempted_at` when available, and the authorized opaque `destination` fingerprint. A missing attempt time is JSON `null`. Results cover the organization's bound engagement and the destination fingerprint in the owner-selected configuration. Use a protected terminal and retain these fields only for the period the custodian permits.

| State | Custodian action |
| --- | --- |
| `pending` | The eligible receipt has no recorded submission. The approved dispatcher can evaluate its current authority. |
| `sending` or `uncertain` | An attempt may have reached the receiver. Look up the original receipt with the destination owner before taking another action. |
| `failed` | The recorded request failed. Check the destination for that receipt and document its disposition. |
| `accepted` | The HTTP receiver accepted the request. The authorized owner still needs to verify the actual downstream action. |

Use `next_cursor` verbatim to read another page of the same query. Page size ranges from 1 to 100. A new query takes a new snapshot of arrivals. Continuing an earlier query excludes newer arrivals and skips items confirmed while paging. Repeating a read on unchanged records returns the same result and preserves all decision and delivery state. An empty page uses `"receipts": []` and `"next_cursor": null`. An invalid, stale-scope or foreign cursor is refused. Start a fresh lookup with the intended private configuration.

```sh
python -I -S -B opentour-slack.pyz slack /private/customer-pilot/worker.json --unresolved-receipts --cursor PREVIOUS_PAGE_CURSOR
python -I -S -B opentour-slack.pyz slack /private/customer-pilot/worker.json --receipt OPAQUE_RECEIPT
```

A current installation can reopen its permitted owner-local decision evidence by receipt. After withdrawal the custodian can inspect each unresolved disclosure using its minimal delivery fields. The separate custody ledger remains authoritative when older working databases are restored. Previously queued work with no attempt stays held. A receipt-only closeout returns only its retained closure record. For historical rows without a provable engagement and destination the listing reports `"legacy_scope": "unknown"`. The custodian reconciles these historical obligations against its private closeout inventory.

The custodian first verifies the independent inventory of recipients and copies, then records its opaque evidence reference on the `inventory-review` item. A settled closeout records `"export_disposition": "zero_exports"` only when the private journal shows zero known or possible Slack and Zapier exposures, no unknown history, and the inventory has no destination item. This fact remains in a receipt-only closeout. If a transmission may have occurred, recover the original receipt or record its outstanding disposition and owner using the independent destination evidence.

Closeout also records obligations for private intake and benchmark registers, starter and live
grants, live evidence and export manifests, and readiness/repair receipts. SQLite sidecars and
broken links count as present. Inventory discovery inspects filenames only; it does not reopen
raw policies, decisions or verifier outcomes after withdrawal. The custodian must separately
account for the raw exports and policy files named by private manifests, backups and remote
copies. These obligations start unresolved with the recorded custodian and due date. A claimed
deletion, expiry or return is refused while the corresponding local state remains present;
record the actual authorized transfer or retention obligation instead. No inventory operation
deletes these files. Restoring a previously deleted artifact reopens its obligation on reconcile.

When the destination owner independently verifies the exact downstream action, record the authorized private evidence reference. Recording this confirmation is an explicit operation separate from reading a page.

```sh
python -I -S -B opentour-slack.pyz slack /private/customer-pilot/worker.json --confirm-zapier OPAQUE_RECEIPT --downstream-ref PRIVATE_OWNER_REFERENCE
```

Reopen the receipt and repeat the unresolved search. A confirmed item leaves the list. Disputed outcomes remain with the named custodian under the agreed support and retention terms.
