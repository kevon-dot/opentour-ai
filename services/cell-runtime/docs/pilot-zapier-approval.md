# Approve a specific repair with Zapier’s Slack Request Approval

## Versioned recipe for a private installation

Use recipe v1 with worker policy v2. The owner enables it only after witnessing the installed bindings. The administrator needs multi-step Zaps, Webhooks, a customer-owned private Platform integration, Slack access to the private channel and an authenticated read-only tenant evidence viewer. Verify these entitlements on the actual account. [Catch Hook plan and setup](https://help.zapier.com/hc/en-us/articles/8496288690317-Trigger-Zaps-from-webhooks) Template sharing is optional.

1. Make one isolated Zap, Slack connection, signing connection, return transport, fresh 32-byte key and owner policy per customer. Set the worker and frozen plan to `zapier_slack`. The installed private channel, workspace, named reviewer, evaluation recipient and current source/review grants come from the owner settings. Pin the published version. The reviewer can read evidence and vote, the operator can reconcile status, and only the installation administrator may edit connections, mappings or secrets.
2. Copy the dummy binding below, replace it locally and compare it with the owner policy. Build the field map with `node integrations/zapier/import-approval-recipe.cjs binding.json zapier-approval.json`. Record the printed revision and manifest SHA-256. Keep the map private. Run the worker check-config command below as well. Neither command contacts a provider.
3. Register the three supplied private app actions. Bind Webhooks → Catch Hook to the locally fingerprinted secret URL. Its versioned input contains only schema_version, request_id, nonce, expires_at and request_signature. Map those five fields to **Verify Approval Reference** on the pinned signer connection. Stop on any failure. The worker retains immutable candidate and audience bindings locally.
4. Configure Slack → Request Approval on the selected Slack connection. Apply every field from the printed map. Choose the channel and exactly one Approver using installation settings. For the two dynamic values, map **id** and **expiry** from the verify step. Leave optional post-confirmation URLs empty. The Details URL resolves an opaque reference. Every member can open it. The tenant viewer must authenticate the session, call the worker's read-only `ApprovalBridge.details` with authenticated team and actor, and render only the authorized review. Never derive identity from the URL. Wait if this viewer is unavailable. Open evidence, browser navigation and Details grant no vote.
5. Map those original five trigger fields to **Sign Verified Approval Reference**. Map status only from Slack's Status output. Map an observed authenticated actor ID if the installed version exposes one. Otherwise leave it empty and require the witnessed one-person policy described below. Return signed_decision verbatim through the bound private transport. Zapier documents Status changing from Pending to Approved or Declined when decline is enabled, with conditional branching. Its reviewer guide also describes a decision halting the Zap. If the installed decline stops before the signer, there may be no downstream event. In that case hold, reconcile through independently authenticated terminal readback, and never manufacture a callback from a handle or an operator's observation.
6. Before enabling, compare posted body, mobile notification, screen-reader fallback, Details denial, full history and exports with the approved fields. The worker emits the single-button invitation. The importer renders the configured card fields and expected fallback. Zapier documents an acting handle in the terminal channel message and exposes no fallback override in the setup guide. If the audience grant excludes that handle, or the installed preview discloses extra data, keep the Zap disabled. Do the same if the installed plan lacks a required feature. Record the published version and the authorized/denied reviewer witnesses separately.

```json
{"slack_connection":"slack-example","signer_connection":"signer-example","return_connection":"return-example","details_origin":"https://review.example.invalid","team":"TEXAMPLE","channel":"CEXAMPLE","reviewer":"UEXAMPLE","zap_id":"example-zap","zap_version":"example-v1"}
```

During rotation, disable the Zap and worker policy, fence pending runs, reconcile outstanding returns privately, change the hook fingerprint and signing secret in both locations, and re-witness the new pinned version before a fresh inspection. The vendor expires pending runs after 30 days and superseding the Zap version prevents old pending runs from continuing. The worker's earlier expiry controls. [Setup fields and limits](https://help.zapier.com/hc/en-us/articles/35891060646413-How-to-set-up-Slack-s-Request-Approval-action-in-Zapier) · [Member interaction and terminal visibility](https://help.zapier.com/hc/en-us/articles/35890982165389-How-to-interact-with-Slack-s-approval-bot)

## Reproduce the generated checks

The baseline is commit `609f34e18671967d22307910f8e67dedf2585c9f`, Git tree `fcc20091745f4076f2d16231c4c8100eaff1a0db`. On Linux with pinned Python 3.12.16 and Node 24.22.3 run `python -m pytest -q services/cell-runtime/tests/pilot/test_zapier_approval.py`. The two recipe vector IDs are `test_versioned_recipe_two_installations[alpha]` (positive and replay) and `test_versioned_recipe_two_installations[beta]` (uncertain dispatch and decline). Both exercise inspection, preview, repeated Continue, private Details, wrong bindings, unsigned results and authenticated Pending. The generated accounts use distinct organizations, Slack teams, channels, reviewers, recipients, hook fingerprints, keys and connections. The cross-install vector rejects the other account’s request reference and signed return. Reopening evidence shows the existing request or receipt. An uncertain or failed dispatch calls for reconciliation of the original request. The ineligible managed status vector also exercises withdrawal. The secret remains in connection authentication. Only the opaque request reference and expiry enter authored channel text. Raw case text, candidate digest, recipient, reviewer and signer key stay out of that text and its expected fallback.

## Register the supplied private signing action

Use a customer-owned private Zapier Platform integration. Place the supplied `integrations/zapier/slack-approval-signer.cjs` in its project root. The standard integration entry point can register it as follows. An existing private app can register the same action in its `creates` object and configure the same connection fields.

```javascript
const { action, authentication, verifyAction, referenceSigner } = require("./slack-approval-signer.cjs");
module.exports = {
  version: require("./package.json").version,
  platformVersion: require("zapier-platform-core").version,
  authentication,
  creates: { [action.key]: action, [verifyAction.key]: verifyAction, [referenceSigner.key]: referenceSigner }
};
```

Validate the private integration with `zapier-platform validate` and publish its new private version to the customer’s authorized account through their normal deployment process. Create its private connection with the same random 32-byte signing key, pinned Zap ID/version, workspace ID, private channel ID and reviewer as the local policy. The key is a masked custom connection field. Do not map it from a trigger or action output. The action checks the signature on the complete incoming request and signs the observed provider status. Give signing connection and Zap editor access only to trusted administrators. [Zapier’s platform CLI](https://github.com/zapier/zapier-platform/blob/main/packages/cli/docs/cli.md)

## Configure the owner workstation

Use the existing engagement directory, owned by the custodian with mode `0700`. Keep `zapier-approval.json`, its signer file, and incoming decision files at mode `0600`. Configure these exact policy fields. Replace each bracketed value before enabling the policy.

```json
{
  "schema_version": "opentour.pilot.zapier-slack-policy.v2",
  "enabled": true,
  "organization": "<enrolled organization>",
  "engagement": "<current engagement ID>",
  "team": "<Slack workspace ID>",
  "channel": "<private Slack channel ID>",
  "reviewer": "<named reviewer Slack user ID>",
  "audience": "<managed plan audience>",
  "zap_id": "<witnessed Zap ID>",
  "zap_version": "<witnessed published version>",
  "approver_mode": "verified_actor",
  "setup_attestation": "<att- followed by 16 to 64 lowercase hexadecimal characters>",
  "key_file": "zapier-signer.json",
  "expires_at": 0,
  "hook_fingerprint": "<SHA-256 of the exact approved HTTPS Catch Hook URL>"
}
```

Use `verified_actor` only for an observed authenticated actor output. For a provider version with no such output, `verified_single_approver` requires witnessed acceptance by the named reviewer and rejection of a second user on that exact version.

Set `expires_at` to the agreed future UTC Unix second. The signer file contains exactly `{"secret_hex":"<64 lowercase hexadecimal characters>"}`. Generate and transfer the shared key through the customer’s existing secret manager. The hook fingerprint uses the exact URL, including a trailing slash if supplied. In a shell where the secret manager has already set the hook URL, the following prints only its fingerprint.

```sh
python -I -c 'import hashlib, os; print(hashlib.sha256(os.environ["OPENTOUR_ZAPIER_APPROVAL_HOOK_URL"].encode()).hexdigest())'
```

Set `PILOT_DIR` to the private absolute directory and `ORG` to the enrolled organization. Run these from the extracted kit with its supported Python. With the opted-in approval configuration, the running Slack Socket Mode worker checks staged requests and signed returns every 30 seconds. For one supervisor pass use `python -I -B opentour-slack.pyz slack "$WORKER_CONFIG" --flush-approvals` with the real absolute worker configuration path. Each operation can also run independently.

```sh
python -I -B opentour-slack.pyz zapier-approval --directory "$PILOT_DIR" --organization "$ORG" --config "$PILOT_DIR/zapier-approval.json" check-config
python -I -B opentour-slack.pyz zapier-approval --directory "$PILOT_DIR" --organization "$ORG" --config "$PILOT_DIR/zapier-approval.json" flush --all
python -I -B opentour-slack.pyz zapier-approval --directory "$PILOT_DIR" --organization "$ORG" --config "$PILOT_DIR/zapier-approval.json" import-ready
```

For a manual return from the authorized private transport, use the actual absolute private file path.

```sh
python -I -B opentour-slack.pyz zapier-approval --directory "$PILOT_DIR" --organization "$ORG" --config "$PILOT_DIR/zapier-approval.json" import --input "$PILOT_DIR/signed-decision.json"
```

## Resolve a hold

- A successful HTTP handoff means that the hook accepted a request. A missing reply after dispatch produces **uncertain**, and the worker never automatically resends it. Find the original request ID in the approved Zap’s history. A signed terminal result from that request can still be imported during its authorization window.
- Reopening the evidence while a request is current reuses its original challenge. After the request expires, the named reviewer must inspect the evidence again to create a new request. Delayed answers to the original request remain expired.
- Pending leaves the candidate locked. Declined records **changes requested** and the existing approved fallback remains in effect. Approved authorizes only the frozen candidate and audience while the owner’s source/release permissions remain current.
- A stale Zap version, changed policy, invalid signature, mismatched actor or expired presentation keeps the candidate locked. Re-witness the destination and issue the next current authorization and evidence presentation as required. Zapier cancels pending runs when their Zap version is superseded and has its own 30-day outer pending window. OpenTour’s shorter request and grant expiries control local release. [Zapier’s pending approval behavior](https://help.zapier.com/hc/en-us/articles/35891060646413-How-to-set-up-Slack-s-Request-Approval-action-in-Zapier)
- The organization can choose native Slack for a later explicitly authorized plan or choose an opting-out destination. Editing a local setting cannot change the release provider frozen into an existing result.
- A generated offline rehearsal proves parsing, signing, reviewer scope, restart, idempotency and release enforcement. A customer setup witness must separately record the real stock action, one authorized response, one unauthorized attempt, its private return and the exact published Zap version before counting live adoption.
