# Reserve an approved handoff on the private worker

Run this command as the configured operator on the customer-owned worker after
the named reviewer has inspected the exact frozen proposal and recorded a
sealed approval. The custodian assigns the operator `reserve` in the current
[signed handoff permission](pilot-recipient-delivery.md#customer-local-destination-permission).
That permission selects the exact existing proposal, artifact revision and
destination. An approving reviewer needs an explicit `reserve` grant to act
as the operator.

Create an owner-only configuration in the existing private worker directory
beside `training.sqlite3`. Use mode 0700 for the directory and 0600 for the
configuration. The four fields are `schema_version` with value
`opentour.pilot.release-reservation.v1`, `tenant` with the installed
organization, `actor` with the configured operator's opaque identifier, and
`workflow_ref` with the selected existing workflow. The private worker OS
account controls this configuration. Keep it outside hosted triggers.

```sh
uv run --frozen python -m opentour_cell.pilot.release_reservation \
  --configuration "$WORKER_DIRECTORY/release-reservation.json" \
  --operation-key "$OPERATION_KEY"
```

Each successful invocation returns one JSON field, `release_ref`, after the
transaction commits. Keep the same operation key if the response is lost.
A different key for the same accepted release returns the same reference.
Use a fresh key for a separately approved successor. Denials exit 2 with no
reference. If the approval committed before this command could run, the
operator can rerun after restoring the current authorized configuration.

The committed reference is the input to the separate
[recipient handoff](pilot-recipient-handoff-simulator.md). A new reservation starts
queued. The operator command performs no recipient lookup, dispatch
or activation. The local command can be run after approval or on restart.
Automatic invocation from the approval route and installed delivery evidence
are separate integration milestones.
