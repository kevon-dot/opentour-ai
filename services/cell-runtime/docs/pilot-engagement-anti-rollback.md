# Engagement ledger anti-rollback records (OPE-543)

The engagement ledger (`engagement-epochs.sqlite3`), its fence journal and every grant live
in the snapshotted worker volume, `/var/lib/opentour-worker`. Restoring an older snapshot of
that volume could bring back an engagement withdrawn after the snapshot was taken. Deleting
the ledger left nothing in the volume to show that a ledger had existed, so a custody
closeout in that directory reconciled as if the engagement had never used one.

The ledger now keeps two kinds of record outside the snapshot, in
`/var/lib/opentour-antirollback`. That is the worker-host state directory that OPE-577
reserves. `OPENTOUR_ANTI_ROLLBACK_DIRECTORY` overrides the location. Tests always set it to a
temporary directory.

## What this protects against, and what it does not

The records defend against a **snapshot restore or an accidental rollback** of
`/var/lib/opentour-worker`. They are **worker-attested at most**. Every record carries the
labels `"assurance": "worker_attested"` and `"protects_against": "snapshot_restore_only"`.

They are **not tamper-proof**. Both worker units run as the same uid. That uid can ptrace the
other unit, rewrite the shared volume, and wipe or rewrite the anti-rollback directory. A
hostile process running as the worker uid can therefore undo everything described here.
Defending against such a process needs one of two things:

- a separate uid for the anti-rollback writer, or
- an external anchor, such as the customer-witnessed or signed records of OPE-566/OPE-583.

Neither is part of this slice.

## Records

Both records use the `opentour.pilot.trust-state.v1` format of OPE-566's `TrustStateStore`:
one canonical JSON file per key under `records/`, named by the key's SHA-256. They take the
same writer lock, `pilot-trust-state.lock`. They have their own namespaces, so the format
does not change. The ledger writes them with the standard library only, which keeps the
packaged worker's import closure unchanged.

| Key | Written | Binds |
|---|---|---|
| `engagement_ledger_marker:<real path>` | Atomically, when the directory's first grant is ingested (`record_engagement` on a ledger with no history), before that ingest commits | The directory's real path, device and inode, and the random store id kept in the ledger database |
| `engagement_revocation:<store id>:<org>` | For every withdrawal: after the fsynced fence journal entry, before the database fence commits | The highest revocation epoch for the organization (as the record's sequence) and the engagement epoch it terminated |

Times are the worker's own clock. No caller supplies one.

## When the worker holds

The ledger checks the records at startup and before every operation, live writes included.
Custody checks the marker when the ledger is missing. Each case below holds the ledger; for
custody, it holds the closeout with `engagement_ledger_unavailable`.

| Situation | Result |
|---|---|
| The marker exists but the ledger database is gone | `anti_rollback_ledger_missing`. Nothing is recreated, and the custody closeout holds |
| The ledger has history but no marker | `anti_rollback_marker_missing`. The marker is never re-derived from the volume it protects |
| The marker does not match the directory (path, device, inode) or the ledger's store id | `anti_rollback_store_mismatch` |
| The marker or a revocation record is not exactly as written | `anti_rollback_marker_corrupt` / `anti_rollback_record_corrupt` |
| An organization's journal is behind its revocation record (a restored pre-withdrawal snapshot, or an emptied journal) | `anti_rollback_volume_behind` |
| The journal entry at the recorded revocation epoch names another engagement epoch | `anti_rollback_revocation_conflict` |
| The location is unset, missing, a link, not a private directory owned by the worker uid, or inside the ledger directory | `anti_rollback_unavailable` / `anti_rollback_inside_snapshot` |
| The revocation record is not durable when a slot would be released | `anti_rollback_revocation_not_durable`. The slot stays held |

A refused call writes nothing. If the location is unusable at the first-grant ingest, the
ingest is refused before anything is written. If it is unusable at a withdrawal, the
withdrawal is refused before the fence is journaled, because the record lock is taken first.
An I/O error while writing the record, after the journal entry, leaves the same state as a
crash at that point: the slot stays held, and retrying the withdrawal completes it.
`restore_backup` checks a backup against the records before it replaces the database.

One state is allowed: a marker beside an empty ledger of the same store. That is the crash
window between the marker write and the first commit, and the ingest may proceed.

## Operational consequences

- Every ledger operation, and every custody closeout whose directory has no visible ledger,
  needs a usable anti-rollback location. On the worker host, systemd creates it. A run
  outside the worker host, such as a local kit run, must point
  `OPENTOUR_ANTI_ROLLBACK_DIRECTORY` at a private directory that is not inside the
  directory it protects.
- Never copy the anti-rollback directory into a snapshot, restore it, or reset it.
  `snapshot_boundary.py check` already refuses a snapshot that contains it.
- A ledger created before this slice, with history but no marker, holds with
  `anti_rollback_marker_missing`.

## Still open

- **Re-baselining.** Recovering after the anti-rollback state is lost, and adopting a ledger
  created before this slice, needs a supervised re-baseline. This slice does not decide or
  build that procedure.
- **Withdrawn mandate ids.** These are decided for the same store but not recorded yet.
  Which mandate stores they cover, and where the startup comparison hooks into them, is not
  specified.
- **Per-epoch rotation.** Rotating the training store and the custody closeout per epoch is
  a separate slice.
- **Stronger defences.** A separate uid or an external anchor is needed against a hostile
  same-uid process (OPE-566/OPE-583).
