# Persist an approved artifact release intent

The worker release register stages an already-approved artifact for its
authorized destination. It binds the existing approval, exact proposal and
artifact revision, current approval grant, workflow and engagement. The link
between a caller's operation key and the canonical release is committed in the
same transaction as its release row. Retrying that intent under a second key
reuses the original release reference. A changed intent under the same key
conflicts, and a changed recipient requires a new approval.

## Worker contracts

The authorization adapter resolves the current human approval and caller role
using the existing engagement store transaction. Selectors carry workflow,
proposal and approval references. The resolved approval supplies the actual
destination, artifact revision and expiry. Readers, dispatch operators and
reconcilers receive separate permissions through that adapter.

The recipient adapter reads the independently authenticated destination for the
exact release reference, destination and artifact digest. It binds the
observation to the worker's fresh opaque lookup reference. A verified absence
freshly observed during the lookup can admit the first worker claim. A committed
claim stays delivery-unconfirmed until current independently verified readback.
A missing or ambiguous lookup keeps the claim held. Status inspection reads the
persisted register and has no receiver I/O.

The recipient worker uses the release reference as its deduplication key and
checks current dispatch authority immediately before its effect. Reconciliation
requires the same exact artifact digest and destination and a receiver-issued
receipt with observed and effective times. A fresh destination lookup at claim
also recovers an already received item when the local register was restored
from a preclaim backup.

The [generated conformance tests](../tests/pilot/test_release_outbox.py) exercise
the transactional register with an independently persisted recipient simulator.
Cases include three equivalent reservations, competing claims, a lost response
after the recipient writes, two status reads, one reconciliation, restored
backup, wrong destination/digest, stale observation and withdrawal during the
lookup. The native approval producers, remote receivers, deployment workflow and
custody handoff consume the register through their separately owned adapters.
