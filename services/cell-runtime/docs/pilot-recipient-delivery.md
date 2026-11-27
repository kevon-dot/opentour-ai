# Reconcile delivery from independent recipient custody

The local receiver binds one enrolled signed package, its recipient and owner
trust anchor, and the current release's tenant, engagement, proposal revision
and artifact digest. The authenticated sender supplies the committed release
reference. The destination id comes from the recipient entitlement and the
pinned owner key. A different tenant or package cannot access that slot.

The sender claims the durable outbox entry before offering the package. The
receiver verifies the existing signed handoff, stages the bytes and receipt,
and checks both the recipient grant and sender claim at publication. The
recipient stores one complete directory per signed specification and
destination. Each lookup checks the exact stored bytes and returns the
opaque receipt, destination and observed time. Unknown or corrupt state is
held. The sender's status reads only inspect the outbox.

The generated qualification uses a real managed comparison, private Slack
approval, encrypted package, signed recipient entitlement and separate
recipient directory. It checks a lost response after the receiver installs,
repeated delivery, sender restart, readback, independent replay and fallback,
wrong scope and artifact, and withdrawal before the final write.

The hosted Zap still needs the authenticated fetch binding and versioned
workflow. The separate, explicitly authorized test activation and its
rollback retain their own evidence requirements.
