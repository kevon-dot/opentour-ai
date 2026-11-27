# Recover pending approval requests locally

This is the owner-local reconciliation part of OPE-414. It uses the existing signed approval
bridge and its existing tenant policy. It does not install a Schedule by Zapier job, send a Slack
message, provision a connection, or establish installed-provider qualification. OPE-435 owns the
installed reconciler/cutover smoke. The saved OPE-548 owner/custodian authority now produces the
replacement generation and immutable reconciliation intents consumed here. Fresh successor
presentation and release-effect consumers are a separate integration requirement; this source
stack does not yet prove the successor's positive release path.

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
Catch Hook acceptance alone also cannot close remote uncertainty: the approval action must supply
an authenticated Pending or terminal observation. Repeated summaries do not count as retry attempts
or update the last observation time. A later Pending or contradictory terminal event cannot rewrite
an authenticated terminal provider status.

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
   completion. The same reviewer must remain bound: reviewer changes use the saved-authority
   sequence below. Direct version, grant or signer drift outside this sequence cannot
   issue or dispatch requests under the old active installation epoch.
5. Open and inspect the exact current evidence again, then explicitly issue the new request. Reusing
   a prior epoch's journaled request-presentation ID is refused even while its old time window remains
   open. Proving freshness for every presentation, including ones never journaled as a request,
   still needs the presentation metadata integration described below. The request has a
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

## Read back an expired or paused ordinary request

An admitted request with a lost reply can outlive the local review deadline. Ordinary
`import` must continue to reject its old vote. For an authentic original provider observation,
use `observe-fenced-status --input /private/tenant/signed-old-status.json`. This owner-local
operation requires an expired request, an existing valid nonce fence, or a durably paused
installation. It refuses an active unfenced request and a completed local decision; use the
existing import path for a timely current vote.

The observation verifies the archived original policy, signer, exact nonce and Zap revision,
event time and named actor. It fences the nonce before recording Pending/Approved/Declined as
historical provider state, without creating a release decision, sending a reminder or dispatching
anything. A worker stop between fencing and the journal commit preserves an unknown hold; replay
the same authenticated observation after restart. An unsigned status or unavailable original key
cannot resolve that hold. A contradictory terminal observation cannot rewrite authenticated
history. Its separate immutable digest receipt quarantines the request across restart; neither
cutover nor a new request can bypass that hold. Repeated conflicting observations retain one
receipt and owner-action item. The authorized operator must investigate the original provider
evidence; this path offers no automatic conflict override. Keep saved reviewer replacement on `observe-replaced-status` and its saved-authority path.

Once every ordinary request is accounted for, continue the cutover drill and obtain a fresh actual
inspection and a new nonce. The old observation never transfers a vote to the new request. A retained terminal-event reservation
must match the observed event identity; conflicting evidence is quarantined without inferring the
original vote from a digest. An earlier Pending observation cannot settle that later reservation.
Resolving remote uncertainty does not restore lost current authority: an authority-loss fence still
requires its separate authorized recovery or saved reviewer-replacement path.
`test_fenced_approval_readback.py` exercises generated lost replies, expiry, pause, restart, denial,
conflicting history and the real bundled signer. These source tests do not qualify an installed
provider readback or a customer delivery.

## Saved reviewer replacement

Keep the original policy and signer available, including their private filenames. An authorized
workflow owner assigns the successor and the separately authorized custodian saves the canonical
settings revision through OPE-548. These role bindings can name one person. Assignment does not
transfer a vote, fabricate an inspection, or qualify the remote provider's outcome.

1. Under the original installed approval policy, run `reconcile-reviewers --authority
   /private/tenant/settings-authority.json --actor <operator>`. The current operator/custodian and
   saved authority are checked. Each bounded queue page verifies the producer's exact sealed
   request fingerprints, pauses dispatch, preserves completed decisions, and projects its nonce
   fences and private owner-action outbox. The local projection commits before acknowledging the
   producer. Repeat until `remaining` is zero; a restart between projection and ACK is idempotent.
2. An admitted request without authenticated approval-action status stays held even if an old
   version had already marked it superseded or expired. Preserve the original marker. Use
   `observe-replaced-status --input /private/tenant/signed-old-status.json` only for an already
   projected request. It verifies the original policy, signer, nonce and actor. A late observation
   can resolve remote uncertainty after request expiry but never creates a release decision.
   Delayed clicks continue to fail through ordinary `import`.
3. Apply only the saved current reviewer to the approval policy, then run
   `complete-reviewer-replacement --authority /private/tenant/settings-authority.json --actor
   <operator>`. Every current proposal must have its exact local projection and current saved
   grant. Outstanding remote outcomes or missing original authority remain held. A verified
   completed release retains its original actor and does not infer an otherwise unknown provider
   status. A repeated adoption returns the same epoch. Zap revision/key changes still use their
   separate cutover sequence.
4. Obtain the successor's actual fresh private evidence and explicit confirmation before issuance.
   Sealed requests bind generation and reviewer-grant revision; issuance, dispatch, callbacks and
   reminders require those values to match the adopted installation and current saved authority.
   Same-reviewer renewal and A → B → A cannot bypass adoption. Dispatch rechecks request expiry
   after obtaining the engagement writer, and retains that writer through the bounded submission.
   If expiry is observed during admission-file synchronization before any submission, a durable
   local never-sent receipt permits expiry recovery. A crash without that receipt remains unknown.

The release-gate/Slack integration must preserve both generation fields and a trustworthy
installation-epoch or presentation-sequence boundary in the actual sealed presentation, and recheck
the request deadline under the final release writer. These consumers and
their end-to-end successor tests remain required before local OPE-414 completion. No placeholder
inspection, local projection ACK or signed provider status substitutes for them.

## Upgrade an older private journal

Earlier bridge versions did not retain original policy or signer fingerprints. If a prior request's
exact persisted identity is present but original verification material is missing, its lane becomes
an explicit `original_request_policy_required` or `original_request_signer_required` hold. Unrelated
current requests remain usable. This compatibility hold grants no event authority, and cutover,
adoption or reissue of the affected proposal cannot bypass it. Tampered requests and mismatched
signer fingerprints fail validation.
Historical release recognition uses the exact preserved actor, artifact and presentation without
reopening a withdrawn source mandate. If multiple legacy nonces shared that exact presentation,
the old receipt cannot establish which request completed; an event reservation alone is insufficient.
The ambiguous request receives no completion credit and any remote uncertainty remains held.

Restore the original approved policy and its original private signer, then use
`recover-request-authority --request-id <ref> --original-policy /private/tenant/original-policy.json`.
The command verifies the original seal against the exact persisted request before restoring the
archive. An expired original policy can authenticate historical identity; it never becomes current
dispatch authority. A previously verified, closed wrapper can retain historical identity after
key retirement, but a new old-provider event still requires the original key. If the original
material cannot be recovered, leave the visible hold and obtain the missing original material from
the authorized operator. Do not substitute database row values for cryptographic verification.
If rotation reused the current signer's filename, place the original secret in a different private
file and add `--original-key /private/tenant/archived-signer.json`. The verified historical key
reference is stored separately by policy revision; the original policy and current signer remain
unchanged. Do not temporarily overwrite the current signer to restore an old request.

## Local conformance

Run `test_reviewer_reconciliation.py`, `test_approval_reconciliation.py` and `test_zapier_approval.py` under the pinned,
guarded Python test runner with Node 24.22.3 available for the actual bundled signer. The tests use
generated tenants and real signed local bridge calls, with fake delivery functions that make no
external request. They cover initial foreign scope, cursor isolation, restart after claim, missing
response, no fresh nonce after an expired unknown attempt, timing boundaries, authenticated Pending
readback over repeated polls, old/new version callbacks, concurrent approve/decline, concurrent
cutover/callback, revocation, interrupted invalidation, transient disk failure and stale-reminder
starvation. Saved replacement tests use the actual signed settings producer and real local bridge,
including CLI projection, unknown-status recovery, old signer retirement and legacy archive holds.
The successor's positive path remains unobserved until the actual presentation and release consumers
join this producer; generated settings edits alone cannot qualify that path.

The operator guide is a source artifact. The executable is already part of the existing portable
`zapier_approval` module; including this guide, `owner_settings` and `reviewer_assignment` in the kit
remains a serialized packaging change. Source CLI tests do not establish a packaged installation.
