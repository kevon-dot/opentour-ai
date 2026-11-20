import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { channel } from 'node:diagnostics_channel';
import { mkdtemp, realpath, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { it, mock } from 'node:test';

mock.module('server-only', { namedExports: {} });
const { createEcsTaskCredentialProvider } = await import('./ecs-task-role.js');
const taskRoleArn = 'arn:aws:iam::000000000000:role/generated-bff';
const relativeUri = '/v2/credentials/01234567-89ab-cdef-0123-456789abcdef';
let metadataRole = taskRoleArn;
const fetchMetadata = mock.fn(async (input: RequestInfo | URL) => {
  assert.equal(String(input), `http://169.254.170.2${relativeUri}`);
  return Response.json({ RoleArn: metadataRole, AccessKeyId: 'fixture-key-id',
    SecretAccessKey: 'generated-fixture-material', Token: 'generated-fixture-session',
    Expiration: new Date(Date.now() + 60_000).toISOString() });
});
mock.module('./ecs-task-role.js', {
  namedExports: { createEcsTaskCredentialProvider, fetchEcsTaskCredentials: fetchMetadata },
});
const { register } = await import('../../instrumentation.js');
const { GET: ready } = await import('../../app/health/ready/route.js');
const { GET: signin } = await import('../../app/auth/signin/route.js');
const { GET: callback } = await import('../../app/auth/callback/route.js');

const profile = {
  origin: 'https://app.example.invalid',
  callbackURI: 'https://app.example.invalid/auth/callback',
  signedOutPath: '/signed-out',
  returnPaths: ['/', '/signed-out'],
  providerOrigins: ['https://api.workos.com'],
  issuer: 'https://identity.example.invalid/',
  audience: 'https://api.example.invalid',
  jwksURL: 'https://api.workos.com/sso/jwks/client_fixture',
  clientId: 'client_fixture',
};
const approvedEndpoint = 'https://fixtureapi-vpce-01234567.execute-api.us-east-2.amazonaws.com/dev';
const currentURL = approvedEndpoint + '/internal/browser-identity/v1/sessions/current';
const createURL = approvedEndpoint + '/internal/browser-identity/v1/flows/create';
const bodyHeaders = { 'cache-control': 'no-store' };
const canonicalMissing = { code: 'SESSION_INVALID', message: 'The identity operation could not be completed.',
  request_id: '0'.repeat(32), retryable: false, field_errors: [], current_revision: null };

it('registers the Node server and bases readiness on a bounded signed private read', async (t) => {
  const phases: unknown[] = [];
  const observations = channel('opentour:bff-readiness:v1');
  const recordPhase = (phase: unknown) => { phases.push(phase); };
  observations.subscribe(recordPhase);
  t.after(() => observations.unsubscribe(recordPhase));
  const dir = await mkdtemp(join(await realpath(tmpdir()), 'opentour-bff-runtime-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const file = join(dir, 'binding.json');
  const bytes = JSON.stringify({ profile, gateway: { approvedEndpoint, region: 'us-east-2',
    restApiId: 'fixtureapi', vpcEndpointId: 'vpce-01234567', taskRoleArn } });
  await writeFile(file, bytes, { mode: 0o400 });
  const env = { NEXT_RUNTIME: 'edge', OPENTOUR_BFF_BINDING_FILE: file,
    OPENTOUR_BFF_BINDING_SHA256: createHash('sha256').update(bytes).digest('hex'),
    OPENTOUR_WORKOS_API_KEY: 'fixture-key', AWS_CONTAINER_CREDENTIALS_RELATIVE_URI: relativeUri };
  for (const [key, value] of Object.entries(env)) {
    const prior = process.env[key];
    process.env[key] = value;
    t.after(() => { if (prior === undefined) delete process.env[key]; else process.env[key] = prior; });
  }

  let canonical = false;
  let time = 1;
  t.mock.method(performance, 'now', () => time);
  const transport = t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    assert.equal(request.method, 'POST');
    const payload = await request.text();
    assert.equal(request.headers.get('x-amz-content-sha256'), createHash('sha256').update(payload).digest('hex'));
    assert.match(request.headers.get('authorization') ?? '', /\/us-east-2\/execute-api\/aws4_request,/);
    if (request.url === createURL) {
      assert.deepEqual(Object.keys(JSON.parse(payload)), ['flow']);
      return Response.json(null, { headers: bodyHeaders });
    }
    assert.equal(request.url, currentURL);
    const data = JSON.parse(payload);
    assert.deepEqual(Object.keys(data), ['handle']);
    assert.match(data.handle, /^[A-Za-z0-9_-]{43}$/);
    return Response.json(canonical ? canonicalMissing : { code: 'SESSION_INVALID' },
      { status: 401, headers: bodyHeaders });
  });

  assert.equal((await ready()).status, 503);
  await register();
  assert.equal((await signin(new Request(profile.origin + '/auth/signin'))).status, 503);
  assert.equal((await callback(new Request(profile.callbackURI))).status, 503);
  assert.deepEqual(phases, []);
  process.env.NEXT_RUNTIME = 'nodejs';
  await Promise.all([register(), register()]);
  assert.equal(fetchMetadata.mock.callCount(), 0);
  assert.equal(transport.mock.callCount(), 0);
  const startupPhases = ['runtime_started', 'workos_key_shape_validated', 'binding_validated',
    'role_provider_configured', 'runtime_configured'];
  assert.deepEqual(phases, startupPhases);

  const failed = await Promise.all([ready(), ready(), ready()]);
  for (const result of failed) {
    assert.equal(result.status, 503);
    assert.equal(result.headers.get('cache-control'), 'no-store');
    assert.equal(await result.text(), '');
  }
  assert.equal(transport.mock.callCount(), 1);
  assert.equal(fetchMetadata.mock.callCount(), 1);
  const probePhases = ['probe_started', 'credentials_validated', 'signed_request_prepared'];
  assert.deepEqual(phases, [...startupPhases, ...probePhases, 'probe_refused']);

  canonical = true;
  assert.equal((await ready()).status, 503);
  time += 10_000;
  assert.equal((await ready()).status, 200);
  const runtimeCopy = './bff-runtime.js?route-bundle';
  const copy = await import(runtimeCopy) as typeof import('./bff-runtime.js');
  assert.equal(await copy.bffRuntimeReady(), true);
  assert.equal(transport.mock.callCount(), 2);
  assert.equal(fetchMetadata.mock.callCount(), 2);
  const expectedPhases = [...startupPhases, ...probePhases, 'probe_refused', ...probePhases,
    'probe_accepted'];
  assert.deepEqual(phases, expectedPhases);

  const adapterCopy = './authkit-adapter.js?route-bundle';
  const { configuredHandler } = await import(adapterCopy) as typeof import('./authkit-adapter.js');
  const result = await configuredHandler('signin')(new Request(profile.origin + '/auth/signin'));
  assert.equal(result.status, 303);
  assert.equal(new URL(result.headers.get('location')!).origin, profile.providerOrigins[0]);
  assert.equal(transport.mock.callCount(), 3);
  assert.equal(fetchMetadata.mock.callCount(), 3);
  assert.deepEqual(phases, expectedPhases);

  metadataRole = 'arn:aws:iam::000000000000:role/generated-foreign';
  time += 10_000;
  assert.equal((await ready()).status, 503);
  assert.equal(fetchMetadata.mock.callCount(), 4);
  assert.equal(transport.mock.callCount(), 3);
  assert.deepEqual(phases, [...expectedPhases, 'probe_started', 'probe_refused']);
});
