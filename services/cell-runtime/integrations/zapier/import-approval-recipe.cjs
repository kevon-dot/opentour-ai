"use strict";

const fs = require("node:fs");
const crypto = require("node:crypto");
const path = require("node:path");

const manifest = fs.readFileSync(path.join(__dirname, "approval-recipe-v1.json"));
const recipe = JSON.parse(manifest);
const manifestHash = crypto.createHash("sha256").update(manifest).digest("hex");

function importRecipe(binding, policy) {
  const validRef = /^[A-Za-z0-9_.:-]{1,96}$/;
  const fields = ["slack_connection", "signer_connection", "return_connection", "details_origin",
    "team", "channel", "reviewer", "zap_id", "zap_version"];
  if (Object.keys(binding).sort().join() !== fields.sort().join() ||
      policy.schema_version !== recipe.worker_policy || policy.enabled !== true ||
      !["team","channel","reviewer","zap_id","zap_version"].every(key =>
        validRef.test(binding[key]) && binding[key] === policy[key]) ||
      !["slack_connection","signer_connection","return_connection"].every(key =>
        typeof binding[key] === "string" && validRef.test(binding[key])) ||
      !/^T[A-Z0-9]+$/.test(binding.team) || !/^[CG][A-Z0-9]+$/.test(binding.channel) ||
      !/^[UW][A-Z0-9]+$/.test(binding.reviewer) ||
      !/^https:\/\/[a-z0-9.-]+$/.test(binding.details_origin)) throw Error("Invalid install binding");

  const fieldsFor = (review, expiry) => Object.fromEntries(Object.entries(recipe.fields).map(
    ([field, template]) => [field, template.replace(/\{(\w+)\}/g,
      (_, key) => ({channel: binding.channel, reviewer: binding.reviewer,
        details_origin: binding.details_origin, review, expiry})[key])]
  ));
  const referenceInputs = Object.fromEntries(
    ["schema_version", "request_id", "nonce", "expires_at", "request_signature"]
      .map(field => [field, "trigger." + field])
  );
  const steps = [
    {action: recipe.trigger, binding: "worker hook fingerprint"},
    {action: recipe.verify, connection: binding.signer_connection, inputs: referenceInputs},
    {action: recipe.provider, connection: binding.slack_connection},
    {action: recipe.signer, connection: binding.signer_connection, inputs: {
      ...referenceInputs, status: recipe.status_source, actor_id: policy.approver_mode === "verified_actor"
        ? "Installed authenticated actor output (witness required)" : null
    }},
    {action: "Private decision return", connection: binding.return_connection, input: recipe.private_return}
  ];
  return {
    steps, revision: recipe.revision, manifest_sha256: manifestHash, fields: fieldsFor("{verify.id}", "{verify.expiry}"),
    render(verified) {
      if (!/^za-[a-f0-9]{32}$/.test(verified.id) ||
          !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000Z$/.test(verified.expiry))
        throw Error("Invalid verified output");
      const fields = fieldsFor(verified.id, verified.expiry);
      const text = fields["Approval request message"];
      // Expected authored content for comparison against actual provider accessibility output.
      return {fields, expectedFallback: text};
    }
  };
}

module.exports = {importRecipe};
if (require.main === module) {
  try {
    const [bindings, policy] = process.argv.slice(2).map(file => JSON.parse(fs.readFileSync(file)));
    const {revision, manifest_sha256, fields, steps} = importRecipe(bindings, policy);
    process.stdout.write(JSON.stringify({revision, manifest_sha256, fields, steps}) + "\n");
  } catch {
    process.stderr.write("Approval recipe settings invalid.\n");
    process.exitCode = 1;
  }
}
