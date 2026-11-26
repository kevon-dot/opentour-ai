"use strict";

const { createHmac, timingSafeEqual } = require("node:crypto");

const requestSchema = "opentour.pilot.zapier-slack-request.v1";
const eventSchema = "opentour.pilot.zapier-slack-decision.v1";
const digestPattern = /^[0-9a-f]{64}$/;
const idPattern = /^[a-zA-Z0-9_.:-]{1,96}$/;

function requireValue(condition, message) {
  if (!condition) throw new Error(message);
}

function valid(pattern, value) {
  return typeof value === "string" && pattern.test(value);
}

function sign(key, context, value) {
  const canonical = JSON.stringify(value, Object.keys(value).sort());
  return createHmac("sha256", key).update(context + "\n" + canonical).digest("hex");
}

function connection(bundle) {
  const auth = bundle.authData || {};
  requireValue(valid(digestPattern, auth.approval_hmac_sha256), "Invalid signing connection");
  requireValue(valid(idPattern, auth.zap_id) && valid(idPattern, auth.zap_version), "Missing pinned Zap or version");
  requireValue(valid(/^T[A-Z0-9]+$/, auth.team) && valid(/^[CG][A-Z0-9]+$/, auth.channel), "Missing private Slack binding");
  requireValue(valid(/^[UW][A-Z0-9]+$/, auth.reviewer), "Missing named reviewer");
  return { auth, key: Buffer.from(auth.approval_hmac_sha256, "hex") };
}

function legacyRequest(bundle) {
  const { auth, key } = connection(bundle);
  const data = bundle.inputData;
  const request = {
    schema_version: data.schema_version,
    request_id: data.request_id,
    nonce: data.nonce,
    expires_at: Number(data.expires_at),
    team: data.team,
    channel: data.channel,
    reviewer: data.reviewer,
    run_revision: data.run_revision,
    artifact_sha: data.artifact_sha,
    audience: data.audience
  };
  const now = Math.floor(Date.now() / 1000);
  requireValue(request.schema_version === requestSchema && valid(/^za-[a-f0-9]{32}$/, request.request_id), "Invalid request");
  requireValue([request.nonce, request.run_revision, request.artifact_sha, data.request_signature].every(v => valid(digestPattern, v)), "Invalid request binding");
  requireValue(Number.isSafeInteger(request.expires_at) && request.expires_at > now, "Request expired");
  requireValue(request.team === auth.team && request.channel === auth.channel && request.reviewer === auth.reviewer && valid(idPattern, request.audience), "Request outside connection scope");
  const expected = sign(key, requestSchema + "/outbound", request);
  requireValue(timingSafeEqual(Buffer.from(expected, "hex"), Buffer.from(data.request_signature, "hex")), "Request signature invalid");
  return request;
}

const referenceSchema = "opentour.pilot.zapier-slack-reference.v1";
const referenceFields = ["schema_version", "request_id", "nonce", "expires_at", "request_signature"];

function verifyReference(bundle) {
  const {auth, key} = connection(bundle);
  const data = bundle.inputData;
  const reference = {
    schema_version: data.schema_version, request_id: data.request_id,
    nonce: data.nonce, expires_at: Number(data.expires_at)
  };
  requireValue(reference.schema_version === referenceSchema &&
    valid(/^za-[a-f0-9]{32}$/, reference.request_id) && valid(digestPattern, reference.nonce) &&
    Number.isSafeInteger(reference.expires_at) && reference.expires_at > Math.floor(Date.now()/1000) &&
    valid(digestPattern, data.request_signature), "Invalid review reference");
  const scope = Object.fromEntries(["team", "channel", "reviewer", "zap_id", "zap_version"]
    .map(field => [field, auth[field]]));
  const expected = sign(key, referenceSchema, {...reference, ...scope});
  requireValue(timingSafeEqual(Buffer.from(expected, "hex"), Buffer.from(data.request_signature, "hex")),
    "Reference outside connection scope");
  return reference;
}

function signDecision(z, bundle) {
  const {auth, key} = connection(bundle);
  const data = bundle.inputData;
  const request = data.schema_version === referenceSchema ? verifyReference(bundle) : legacyRequest(bundle);
  const now = Math.floor(Date.now() / 1000);
  const statuses = { pending: "Pending", approved: "Approved", declined: "Declined" };
  const status = typeof data.status === "string" && Object.hasOwn(statuses, data.status.toLowerCase()) ? statuses[data.status.toLowerCase()] : null;
  requireValue(status, "Unsupported Slack status");
  const actor = data.actor_id || null;
  requireValue(actor === null || actor === auth.reviewer, "Decision actor outside connection scope");
  const event = {
    schema_version: eventSchema,
    request_id: request.request_id,
    nonce: request.nonce,
    zap_id: auth.zap_id,
    zap_version: auth.zap_version,
    event_id: request.request_id + "." + status.toLowerCase(),
    decided_at: now,
    status,
    actor_id: actor
  };
  const envelope = { event, signature: sign(key, eventSchema, event) };
  return { id: event.event_id, signed_decision: JSON.stringify(envelope) };
}

const connectionFields = [
  { key: "approval_hmac_sha256", label: "Owner signing secret (64 hex)", type: "password", required: true },
  { key: "zap_id", label: "Pinned Zap ID", required: true },
  { key: "zap_version", label: "Witnessed published Zap version", required: true },
  { key: "team", label: "Workspace ID", required: true },
  { key: "channel", label: "Private Slack channel ID", required: true },
  { key: "reviewer", label: "Named Slack approver ID", required: true }
];
const requestFields = ["schema_version", "request_id", "nonce", "expires_at", "team", "channel", "reviewer", "run_revision", "artifact_sha", "audience", "request_signature"];
const authentication = {
  type: "custom",
  fields: connectionFields,
  test: (z, bundle) => {
    const { auth } = connection(bundle);
    return { id: auth.zap_id, version: auth.zap_version };
  },
  connectionLabel: "{{bundle.authData.zap_id}} / {{bundle.authData.zap_version}}"
};
const action = {
  key: "sign_slack_approval",
  noun: "Decision",
  display: {
    label: "Sign a Slack Request Approval Decision",
    description: "Signs the provider status for the exact protected repair the assigned reviewer inspected."
  },
  operation: {
    inputFields: [
      ...requestFields.map(key => ({ key, label: "Catch Hook " + key, required: true })),
      { key: "status", label: "Slack Request Approval status", required: true },
      { key: "actor_id", label: "Slack-reported action user ID (if provided)" }
    ],
    perform: signDecision,
    outputFields: [
      { key: "id", label: "Decision event ID" },
      { key: "signed_decision", label: "Signed decision JSON" }
    ],
    sample: { id: "generated-pending", signed_decision: "{}" }
  }
};

const referenceInputs = referenceFields.map(key => ({key, required: true}));
const verifyAction = {
  key: "verify_approval_reference", noun: "Review",
  display: {label: "Verify Approval Reference", description: "Check the worker reference before posting."},
  operation: {
    inputFields: referenceInputs,
    perform: (z, bundle) => {
      const request = verifyReference(bundle);
      return {id: request.request_id, expiry: new Date(request.expires_at * 1000).toISOString()};
    },
    sample: {id: "za-00000000000000000000000000000000", expiry: "2030-01-01T00:00:00.000Z"}
  }
};
const referenceSigner = {
  ...action, key: "sign_approval_reference",
  display: {...action.display, label: "Sign Verified Approval Reference"},
  operation: {...action.operation, inputFields: [...referenceInputs, ...action.operation.inputFields.slice(-2)],
    perform: (z, bundle) => {
      requireValue(bundle.inputData.schema_version === referenceSchema, "Invalid reference version");
      return signDecision(z, bundle);
    }}
};
module.exports = {action, authentication, verifyAction, referenceSigner};
