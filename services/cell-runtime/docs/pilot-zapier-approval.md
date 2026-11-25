# Approve a specific repair with Zapier’s Slack Request Approval

A reviewer opens **Inspect evidence** in the private OpenTour Slack app and reads the result for their team. When the frozen plan selects `zapier_slack`, that successful presentation queues a request for the first-party **Slack → Request Approval** action in the customer’s Zap. The reviewer can **Approve this release** or **Request changes**. Only a valid terminal decision for that exact version can clear the local release gate. Approval permits the separately authorized handoff or activation. It does not switch production traffic by itself.

The first ten use the customer’s owner workstation, their existing Slack workspace and an authorized Zapier account. The workstation makes an outbound HTTPS request. An existing private return transport or an explicit local import carries the signed decision back. No public callback listener or AWS infrastructure is required.

## Set up one approved private destination

1. Identify the specific workspace ID, private channel ID, one named release reviewer, audience, source scope, opted-in Zap ID and published Zap version with the customer. Bind the worker setting `approval_provider` and managed plan’s `release_review.provider` to `zapier_slack`. The current Slack grant must include both inspect and review authority. Ordinary permission to share a readout is separate.
2. In a private Zap, use **Webhooks by Zapier → Catch Hook**, then **Slack → Request Approval**. Set the channel to the agreed private channel and set **Approvers** to exactly the named reviewer. Enable the decline button and confirmation dialogs. Label the buttons **Approve this release** and **Request changes**. Include the complete `run_revision`, `artifact_sha`, `audience`, and the `request_id` from the trigger in the message. Suggested wording is “Authorize release of the repair you inspected in OpenTour for audience [audience]. Run [run_revision]. Candidate [artifact_sha]. Request [request_id].” The request contains no individual cases or model weights. [Zapier’s Request Approval guide](https://help.zapier.com/hc/en-us/articles/35891060646413-How-to-set-up-Slack-s-Request-Approval-action-in-Zapier)
3. Add the private **Sign a Slack Request Approval Decision** action described below. Map the signer’s **Catch Hook** fields from the original trigger and its **status** exclusively from the stock Slack action’s Status output. If the connector exposes the real responding user ID, map it to **actor_id** and use the local policy mode `verified_actor`. The published Slack action documentation promises status, not an actor ID. Where it is absent, leave actor ID empty and use `verified_single_approver` only after the owner witnesses one permitted reviewer and a denied second user on that exact published version. Do not substitute an intended reviewer, display name or static actor ID. [Zapier’s documented action outputs](https://help.zapier.com/hc/en-us/articles/35891060646413-How-to-set-up-Slack-s-Request-Approval-action-in-Zapier)
4. Return the signer’s **Signed decision JSON** verbatim through the customer-approved private transport. Save it as `<signer id>.json` in the workstation’s private `zapier-decisions/` directory for unattended import. For example, the signer’s ID ends in `.approved` after approval. Existing private synchronization must preserve owner-only modes and publish complete files by atomic rename. A custodian can also copy that exact JSON to a private file and run the explicit import below. Keep the transport and its retention period within the customer’s permission grant.
5. Record the tested workspace/channel/reviewer, private return, Zap version and permission witness under the owner’s opaque `setup_attestation`. Store the locally approved hook fingerprint with the policy. Supply the actual URL to the approved supervisor through `OPENTOUR_ZAPIER_APPROVAL_HOOK_URL` using its private secret facility. Keep it out of tickets, chat and command history.

## Register the supplied private signing action

Use a customer-owned private Zapier Platform integration. Place the supplied `integrations/zapier/slack-approval-signer.cjs` in its project root. The standard integration entry point can register it as follows. An existing private app can register the same action in its `creates` object and configure the same connection fields.

```javascript
const { action, authentication } = require("./slack-approval-signer.cjs");
module.exports = {
  version: require("./package.json").version,
  platformVersion: require("zapier-platform-core").version,
  authentication,
  creates: { [action.key]: action }
};
```

Validate the private integration with `zapier-platform validate` and publish its new private version to the customer’s authorized account through their normal deployment process. Create its private connection with the same random 32-byte signing key, pinned Zap ID/version, workspace ID, private channel ID and reviewer as the local policy. The key is a masked custom connection field. Do not map it from a trigger or action output. The action checks the signature on the complete incoming request and signs the observed provider status. Give signing connection and Zap editor access only to trusted administrators. [Zapier’s platform CLI](https://github.com/zapier/zapier-platform/blob/main/packages/cli/docs/cli.md)

## Configure the owner workstation

Use the existing engagement directory, owned by the custodian with mode `0700`. Keep `zapier-approval.json`, its signer file, and incoming decision files at mode `0600`. Configure these exact policy fields. Replace each bracketed value before enabling the policy.

```json
{
  "schema_version": "opentour.pilot.zapier-slack-policy.v1",
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
