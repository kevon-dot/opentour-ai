# Run the local approval reconciliation schedule

`reconciliation-recipe-v1.json` defines a disabled schedule for one existing
worker installation. Run its local tick from the owner-managed worker to bind it
to that installation's private directory, approval policy file, organization and
current installation epoch. Select intervals and budgets in the worker's launch
configuration. Keep the arguments, directory and policy private to the worker.

Obtain the epoch from the normal `zapier-approval pending-summary` response after
the bridge has pinned the installation. A scheduler must use that specific epoch.
An epoch change requires an intentional schedule update after the cutover drill.

```sh
python -I -S -B opentour-slack.pyz approval-reconciler \
  --directory "$WORKER_DIRECTORY" --organization "$WORKER_ORGANIZATION" \
  --config "$APPROVAL_POLICY" --installation-epoch "$INSTALLATION_EPOCH" \
  --enabled --poll-interval 60 --reminder-interval 3600
```

Run the command from the extracted kit, or give the full path to the zipapp.
In a source checkout, use `python -m opentour_cell.pilot.approval_reconciler`
with the same arguments. Invoke at the owner-selected polling interval while
the schedule is enabled.
The worker saves the exact opaque references and next cursor for one page before
starting work. It acknowledges a reference after a valid local reconciliation
response. A lost response and a throttle keep the reference for a later attempt.
Each staged action keeps the same identity on retry. The bridge commits its
completion with the actual disposition and outbox effects, so a restart can
acknowledge that action without another reconciliation or reminder.
An actual local disposition of `remote_outcome_unknown` remains held in the
approval journal. It can be observed on later sweeps and requires genuine
provider evidence before reissue. Polling never turns silence into a vote.

The local JSON receipt reports the opaque tenant and sweep references,
installation epoch, page and action counters, and the local inventory coverage.
An acknowledged reference means the worker accounted for it, including a visible
held disposition. Look up pending age, holds, retry counts and expired counts from
the existing scoped `pending-summary` result for the same epoch. Keep only the
opaque identifiers, counts, ages and fixed reason codes in the scrubbed log.

The schedule queues reminders in the bridge's existing durable outbox. The
existing owner-authorized delivery adapter handles claim and delivery confirmation.
An uncertain delivery remains in that adapter's recovery queue. The scheduled
tick performs no delivery or confirmation itself.

When a tick reports `page_limit`, `request_limit`, `deferred` or `timeout`, invoke
it again on the normal cadence. A stopped process can be restarted with the same
owner configuration and directory. The last persisted page and its acknowledged
references resume there. A fresh tick that waited for the existing writer past its
deadline starts no inventory or reconcile work. The deadline controls the start
of new work, and an existing inventory or reconcile operation can take longer.

For cutover, follow the pause, drain, reconciliation and nonce invalidation steps
in [the approval reconciliation guide](pilot-approval-reconciliation.md). Resume
only with the current installed epoch after the owner rebinds the schedule. Do
not discard a held cursor or migrate older references into a new epoch; an
unavailable or configuration-held result requires inspection of the owner ledger.

The Schedule by Zapier mapping requires an installed, approval-scoped connection
with pending-summary and reconcile actions and scrubbed history. This recipe
defines the disabled worker-local schedule and describes the pending hosted
mapping. Provider installation and smoke are tracked by OPE-435.
