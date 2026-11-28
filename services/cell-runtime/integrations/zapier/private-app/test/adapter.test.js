'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const https = require('node:https');
const core = require('zapier-platform-core');
const app = require('../index');
const { connection, argumentsFor, metadata, perform } = require('../adapter');
const z = { errors: core.errors };
const authData = { connection_alias: 'generated-a', origin: 'https://worker.example.invalid', audience: 'generated-intake', access_token: 'T'.repeat(43) };
const historical = { pair_ref: 'pair-a', source_revision: 'trace-1', correction_revision: 'truth-1', policy_revision: 'policy-1', idempotency_key: 'intent-1' };
const starter = { mode: 'starter', creation_ref: 'creation-a', policy_resource_ref: 'policy-a', source_revision: 'source-1', setup_policy_revision: 'setup-1', idempotency_key: 'intent-1' };
const live = { mode: 'live', feed_binding_ref: 'feed-a', source_revision: 'feed-1', idempotency_key: 'intent-1' };

test('exact app exposes only three actions and password-scoped connection', () => {
  assert.deepEqual(Object.keys(app.creates).sort(), ['discover_sources', 'get_run', 'submit_intake']);
  assert.equal(app.platformVersion, '19.3.0');
  assert.equal(app.flags.skipHttpPatch, true);
  assert.equal(app.authentication.fields.find((f) => f.key === 'access_token').type, 'password');
  for (const action of Object.values(app.creates)) {
    assert.ok(action.operation.inputFields.every((f) => !['origin', 'audience', 'access_token', 'url', 'tenant'].includes(f.key)));
  }
});

test('connection rejects URLs with non-TLS, credentials, path, query, fragment or origin normalization', () => {
  assert.equal(connection(authData).origin, authData.origin);
  for (const origin of ['http://worker.example.invalid', 'https://worker.example.invalid/', 'https://user@worker.example.invalid', 'https://worker.example.invalid?x=1', 'https://worker.example.invalid#x', 'https://worker.example.invalid/v1', 'https://worker.example.invalid:0', 'https://WORKER.example.invalid', 'https://worker.example.invalid:443']) {
    assert.throws(() => connection({ ...authData, origin }), /connection_configuration_required/);
  }
  for (const patch of [{ access_token: '' }, { audience: 1 }, { audience: 'bad\nheader' }, { connection_alias: 'private data with spaces' }]) {
    assert.throws(() => connection({ ...authData, ...patch }), /connection_configuration_required/);
  }
});

test('input schema preserves worker historical, both starter and live variants', () => {
  for (const input of [historical, { mode: 'historical', ...historical }, starter, live]) {
    assert.deepEqual(argumentsFor('submit_intake', input), input);
  }
  const { creation_ref, ...rest } = starter;
  assert.deepEqual(argumentsFor('submit_intake', { ...rest, workflow_ref: 'existing-workflow' }), { ...rest, workflow_ref: 'existing-workflow' });
  assert.deepEqual(argumentsFor('discover_sources', { limit: 50, cursor: 'page-2' }), { limit: 50, cursor: 'page-2' });
  assert.deepEqual(argumentsFor('get_run', { run_ref: 'run-a' }), { run_ref: 'run-a' });
});

test('rejects ambiguous modes, extra credentials/tenants, live corrections and invalid paging before network', async () => {
  for (const input of [{ ...starter, workflow_ref: 'also-present' }, { ...live, correction_revision: 'forbidden' }, { ...historical, origin: 'https://other.invalid' }, { ...historical, tenant: 'other' }, { ...historical, access_token: 'leak' }, { ...historical, idempotency_key: 'raw record\n' }, { ...live, mode: 'unknown' }]) {
    await assert.rejects(perform(z, { authData, inputData: input }, 'submit_intake'), /invalid_intake_arguments/);
  }
  for (const input of [{ limit: 0 }, { limit: 51 }, { limit: '1' }, { limit: true }, { cursor: 'https://other.invalid' }, { offset: 2 }]) {
    assert.throws(() => argumentsFor('discover_sources', input), /invalid_intake_arguments/);
  }
  assert.throws(() => argumentsFor('arbitrary_route', {}), /invalid_intake_arguments/);
});

test('response allowlist includes current live catalog and held successor without raw fields', () => {
  const catalog = { sources: [{ source_kind: 'historical', source_ref: 'pair-a', source_revision: 'v1' }, { feed_binding_ref: 'feed-a', binding_version: 'v2', feed_kind: 'independent_outcome', source_revision: 'truth-v2' }], next_cursor: 'page-2' };
  assert.deepEqual(metadata('discover_sources', catalog, {}), catalog);
  const held = { state: 'held', reason_code: 'binding_superseded', successor_feed_binding_ref: 'feed-b' };
  assert.deepEqual(metadata('submit_intake', held, live), held);
  const admittedHold = { run_ref: 'run-a', state: 'held', correlation_ref: 'correlation-a' };
  assert.deepEqual(metadata('submit_intake', admittedHold, historical), admittedHold);
  const status = { run_ref: 'run-a', state: 'held', next_action: 'supply_evidence', reason_code: null, workflow_ref: 'workflow-a', benchmark_ref: 'benchmark-a', version: 1, benchmark_state: 'draft' };
  assert.deepEqual(metadata('get_run', status, { run_ref: 'run-a' }), status);
  for (const value of [{ ...status, source_rows: ['secret'] }, { ...status, run_ref: 'foreign' }, { ...status, next_action: 'raw description with spaces' }, { ...status, version: {} }]) {
    assert.throws(() => metadata('get_run', value, { run_ref: 'run-a' }), /invalid_reference_metadata/);
  }
  assert.throws(() => metadata('discover_sources', { ...catalog, token: 'secret' }, {}), /invalid_reference_metadata/);
  assert.throws(() => metadata('discover_sources', catalog, { limit: 1 }), /invalid_reference_metadata/);
  assert.throws(() => metadata('submit_intake', { ...held, private_record: 'secret' }, live), /invalid_reference_metadata/);
});

function transport(t, status, payload, contentType = 'application/json') {
  const seen = [];
  t.mock.method(https, 'request', (url, options, callback) => {
    const req = new EventEmitter();
    req.destroy = () => req.emit('error', new Error('transport-internal-secret'));
    req.end = (body) => {
      seen.push({ url, options, body: JSON.parse(body) });
      setImmediate(() => {
        const res = new EventEmitter();
        res.statusCode = status;
        res.headers = { 'content-type': contentType };
        res.resume = () => req.emit('close');
        res.destroy = () => req.emit('close');
        callback(res);
        res.emit('data', Buffer.from(typeof payload === 'string' ? payload : JSON.stringify(payload)));
        res.emit('end'); req.emit('close');
      });
    };
    return req;
  });
  return seen;
}

test('one exact owner-bound request, no redirects, no automatic resend and immutable input key', async (t) => {
  const receipt = { run_ref: 'run-a', state: 'queued', correlation_ref: 'correlation-a' };
  const seen = transport(t, 200, receipt);
  const actual = await perform(z, { authData, inputData: historical }, 'submit_intake');
  assert.deepEqual(actual, receipt);
  assert.equal(seen.length, 1);
  assert.equal(seen[0].url, `${authData.origin}/v1/submit_intake`);
  assert.equal(seen[0].options.headers.Origin, authData.origin);
  assert.equal(seen[0].options.headers['X-OpenTour-Audience'], authData.audience);
  assert.equal(seen[0].options.headers.Authorization, `Bearer ${authData.access_token}`);
  assert.deepEqual(seen[0].body, historical);
  assert.equal(seen[0].options.rejectUnauthorized, true); // Ambient env cannot weaken trust.
});

for (const [status, expected] of [[302, 'intake_outcome_unknown'], [403, 'intake_unavailable'], [409, 'idempotency_conflict'], [429, 'intake_rate_limited'], [503, 'intake_outcome_unknown']]) {
  test(`HTTP ${status} has bounded error and no reflected body`, async (t) => {
    const seen = transport(t, status, { error: 'private-response-sentinel', token: authData.access_token });
    await assert.rejects(perform(z, { authData, inputData: historical }, 'submit_intake'), (err) => {
      assert.equal(JSON.parse(err.message).code, expected);
      assert.ok(!JSON.stringify(err).includes('private-response-sentinel'));
      assert.ok(!JSON.stringify(err).includes(authData.access_token));
      return true;
    });
    assert.equal(seen.length, 1);
  });
}

test('malformed and oversized success bodies do not become task outputs', async (t) => {
  transport(t, 200, 'private-response-sentinel'.repeat(2000));
  await assert.rejects(perform(z, { authData, inputData: {} }, 'discover_sources'), /invalid_reference_metadata/);
});

test('published SDK executes app authentication against only discovery and does not patch HTTPS', async (t) => {
  const seen = transport(t, 200, { sources: [], next_cursor: null });
  const original = https.request;
  const tester = core.createAppTester(app);
  const result = await tester(app.authentication.test, { authData });
  assert.deepEqual(result, { connection_alias: authData.connection_alias });
  assert.equal(https.request, original);
  assert.equal(seen.length, 1);
  assert.equal(seen[0].url, `${authData.origin}/v1/discover_sources`);
  assert.deepEqual(seen[0].body, { limit: 1 });
});
