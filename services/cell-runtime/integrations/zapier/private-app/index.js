'use strict';

const { perform } = require('./adapter');
const field = (key, helpText, required = false) => ({ key, type: 'string', required, helpText });
const submitFields = [
  { key: 'mode', type: 'string', required: true, choices: ['historical', 'starter', 'live'], default: 'historical',
    helpText: 'Use historical for an authorized source/correction pair, starter for a policy, or live for an owner-bound feed.' },
  field('idempotency_key', 'Keep this same opaque key when reconciling a lost reply. Never invent a new key to force a retry.', true),
  field('source_revision', 'Exact source revision returned by permitted discovery.', true),
  field('pair_ref', 'Historical only: approved pair reference. No URL or raw rows.'),
  field('correction_revision', 'Historical only: separately authorized correction revision. Omit for live requests.'),
  field('policy_revision', 'Historical only: current admitted policy revision.'),
  field('policy_resource_ref', 'Starter only: authorized policy resource reference.'),
  field('setup_policy_revision', 'Starter only: current setup policy revision.'),
  field('creation_ref', 'Starter only: use exactly one creation_ref or existing workflow_ref.'),
  field('workflow_ref', 'Starter only: use exactly one existing workflow_ref or creation_ref.'),
  field('feed_binding_ref', 'Live only: owner-bound decision or independent-outcome feed from discovery. No correction_revision. Inactive/unconfigured bindings remain held.'),
];
const output = (key, type = 'string') => ({ key, type });
const action = (key, noun, label, description, inputFields, sample, outputFields) => ({
  key, noun, display: { label, description },
  operation: { inputFields, outputFields, sample, perform: (z, bundle) => perform(z, bundle, key) },
});

module.exports = {
  version: require('./package.json').version,
  platformVersion: require('zapier-platform-core').version,
  // Native HTTPS plus this documented flag avoids the core's raw HTTP body logger.
  // Installed bundle/history redaction still requires the OPE-437 provider witness.
  flags: { skipHttpPatch: true, cleanInputData: true },
  authentication: {
    type: 'custom',
    fields: [
      field('connection_alias', 'Opaque owner-assigned test/tenant alias. Not a credential or an authority proof.', true),
      field('origin', 'Owner-approved fixed HTTPS origin, e.g. https://worker.example.invalid. No path, query, redirect or action-supplied URL.', true),
      field('audience', 'Exact audience from the installed worker connection.', true),
      { key: 'access_token', type: 'password', required: true, helpText: 'Scoped worker intake credential. Keep it in this connection, never in Zap fields.' },
    ],
    test: async (z, bundle) => {
      await perform(z, { ...bundle, inputData: { limit: 1 } }, 'discover_sources');
      return { connection_alias: bundle.authData.connection_alias };
    },
    connectionLabel: '{{connection_alias}}',
  },
  creates: {
    discover_sources: action('discover_sources', 'Source catalog', 'Discover Authorized Sources',
      'Lists one bounded page of opaque sources or live feed revisions permitted by this connection.',
      [{ key: 'limit', type: 'integer', helpText: '1–50 entries, default 50.' }, field('cursor', 'Opaque next_cursor from the previous page. No offset or external URL.')],
      { sources: [{ source_kind: 'policy', source_ref: 'generated-policy', source_revision: 'revision-1' }], next_cursor: null },
      [{ key: 'sources', children: ['source_kind', 'source_ref', 'source_revision', 'feed_binding_ref', 'binding_version', 'feed_kind'].map((k) => output(k)) }, output('next_cursor')]),
    submit_intake: action('submit_intake', 'Intake receipt', 'Submit Authorized Intake',
      'Admits one historical pair, policy starter or bound live feed revision. A hold grants no job or release authority.',
      submitFields,
      { run_ref: 'generated-run', state: 'accepted', correlation_ref: 'generated-correlation' },
      ['run_ref', 'state', 'correlation_ref', 'reason_code', 'successor_feed_binding_ref'].map((k) => output(k))),
    get_run: action('get_run', 'Run status', 'Get Authorized Run',
      'Reads bounded current run and benchmark status under the same connection. It does not authorize a release.',
      [field('run_ref', 'Opaque run_ref from the admission receipt.', true)],
      { run_ref: 'generated-run', state: 'held', next_action: 'supply_evidence', reason_code: 'evidence_missing', workflow_ref: 'generated-workflow' },
      ['run_ref', 'state', 'next_action', 'reason_code', 'workflow_ref', 'benchmark_ref', 'version', 'benchmark_state'].map((k) => output(k))),
  },
};
