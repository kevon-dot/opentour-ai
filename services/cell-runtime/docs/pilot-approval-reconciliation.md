# Recover pending approval requests locally

This is the owner-local reconciliation part of OPE-414. It uses the existing signed approval
bridge and its existing tenant policy. It does not install a Schedule by Zapier job, send a Slack
message, provision a connection, or establish installed-provider qualification. OPE-435 owns the
installed reconciler/cutover smoke. OPE-548's saved owner/custodian replacement authority is still
required before a successor reviewer can be installed; that transition deliberately stays held.

Use Python 3.12.16 and the versioned kit's `zapier-approval` command with the same private
`--directory`, `--organization`, and `--config` used for existing approval requests. The configured
organization, engagement, current enabled policy, signer and private file permissions are verified
before the new ledger is created. An installed adapter must derive these bindings from its approved
connection; they must not be accepted as untrusted request arguments.

```sh
python -I -S -B opentour-slack.pyz zapier-approval \
  --directory /private/tenant --organization tenant-ref \
  --config /private/tenant/zapier-approval.json pending-summary --limit 25
python -I -S -B opentour-slack.pyz zapier-approval \
  --directory /private/tenant --organization tenant-ref \
  --config /private/tenant/zapier-approval.json reconcile \
  --poll-interval 60 --reminder-interval 3600
```

`pending-summary` returns opaque request references, disposition, bounded reason, the last
authenticated provider status (or unknown), pending age, retries and tenant-level counts. Follow its
scoped `--cursor` for further pages. The cursor is bound to the installation epoch and a stable
inventory ceiling. It cannot be reused by another tenant or after a completed cutover. Empty inventory
is not evidence of successful provider polling: this local command observes the worker's signed
returns and transport receipts, not the live provider's history.
For each listed reference, `reconcile --request-id <ref>` performs the same idempotent scoped action.
Each reference has its own durable polling interval; an unknown or foreign reference is rejected.

The local timer enforces the configured interval independently of the provider. A successful
reconciliation atomically writes each request disposition and any reminder/owner-action outbox item.
At most one reminder is queued per request per elapsed configured interval, including across clock
bucket boundaries and worker restarts. A trusted signed Pending return can resolve a lost submission
acknowledgement without approving a release. Preserve that authenticated observation across polls.
An unsigned status, HTTP acceptance, timeout or missing acknowledgement never grants release authority.

## Notifications and recovery

`claim-notification` reserves one opaque outbox item and marks its delivery uncertain before returning
it. An authorized private notification adapter may deliver that item using `notification_ref` as its
idempotency key. Only after a verified delivery does that adapter call
`confirm-notification --notification-ref <ref>`. Repeating confirmation recovers the same receipt.
There is no automatic resend of an uncertain claim after a crash. Reconcile the original notification
with its receiver before further action. These local commands do not send notifications themselves.

An invalidated, expired or completed request's stale pending reminder is cancelled so it cannot block
new valid notifications. A durable owner-action item remains visible even if its external notification
fails. A transient local read failure yields a recoverable hold without permanently archiving the
nonce. Affirmative loss of current inspection/review authority fences the old request and records an
owner action. The owner's private channel and successor assignment must come from authorized settings;
this implementation never invents a replacement owner, reviewer or destination.

## Cutover drill

1. Keep the original installed policy and signer available. Import/drain any already-received signed
   terminal decisions with the existing `import-ready` command. Provider status alone is not a vote.
2. Run `prepare-cutover` under the original policy. This pauses new issuance and dispatch before
   inventory. Completed human decisions retain their original receipts. Known unresolved requests are
   superseded and their nonces are fenced. Unknown remote outcomes remain held; do not replace them.
3. Read every page of `pending-summary`. Account for every original request. If a worker stops between
   durable nonce invalidation and the ledger commit, the next authenticated operation repairs the
   disposition and owner-action outbox from that marker. Repeated polls create no extra reminder.
4. Only after the earlier lane is closed, have the authorized installer apply the new approved Zap
   revision to the existing private policy. Run `complete-cutover`. An unresolved remote outcome blocks
   completion. The same reviewer must remain bound: changing the reviewer is held pending the real
   OPE-548 saved authority producer. Direct version, grant or signer drift outside this sequence cannot
   issue or dispatch requests under the old active installation epoch.
5. Open and inspect the exact current evidence again, then explicitly issue the new request. It has a
   new nonce and installation epoch. Replay the old signed callback: it produces no new release. Two
   identical current callbacks recover one release receipt; contradictory callbacks are quarantined
   with their event digest and do not rewrite the earlier completed decision.
6. Record the exact source/kit hash, old and new Zap revisions, epoch, opaque request references,
   outcomes, failed/skipped checks and cleanup. Customer data, credentials and full request envelopes
   stay private. Do not claim receiver-backed exports from a sender-side acknowledgement.

If settings were edited before step 2, the safe outcome is `installation_cutover_required`. Restore
the original approved configuration and perform the drain/fence sequence. Do not weaken the gate or
fabricate a provider status to unblock it. A signed provider readback may resolve an unknown submission;
if no valid readback exists, the request remains held. Past completed effects are preserved.

## Local conformance

Run `test_approval_reconciliation.py` and the existing `test_zapier_approval.py` under the pinned,
guarded Python test runner with Node 24.22.3 available for the actual bundled signer. The tests use
generated tenants and real signed local bridge calls, with fake delivery functions that make no
external request. They cover initial foreign scope, cursor isolation, restart after claim, missing
response, no fresh nonce after an expired unknown attempt, timing boundaries, authenticated Pending
readback over repeated polls, old/new version callbacks, concurrent approve/decline, concurrent
cutover/callback, revocation, interrupted invalidation, transient disk failure and stale-reminder
starvation. The successor's positive path remains unobserved until OPE-548 implements the canonical
saved settings revision; generated settings edits cannot qualify that authority.

The operator guide is a source artifact. The executable is already part of the existing portable
`zapier_approval` module; including this extra guide in the kit remains a serialized packaging change.
