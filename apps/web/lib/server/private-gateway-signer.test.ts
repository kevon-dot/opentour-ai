/** Offline known-answer test for the scoped private gateway signer. */
import assert from 'node:assert/strict';
import { it, mock } from 'node:test';
import { testCases } from '../../tests/web-unit-cases.js';
import type { PrivateBrowserSignInput } from './private-browser-client.js';

mock.module('server-only', { namedExports: {} });
const { IdentityFailure } = await import('./authkit-adapter.js');
const { createPrivateBrowserPorts } = await import('./private-browser-client.js');
const { createPrivateGatewaySigner } = await import('./private-gateway-signer.js');

type SignerOptions = Parameters<typeof createPrivateGatewaySigner>[0];
type TemporaryCredentials = Awaited<ReturnType<SignerOptions['credentials']>>;
const REGION = 'us-east-2';
const REST_API_ID = 'a1b2c3d4e5';
const VPC_ENDPOINT_ID = 'vpce-0123456789abcdef0';
const ORIGIN = `https://${REST_API_ID}-${VPC_ENDPOINT_ID}.execute-api.${REGION}.amazonaws.com`;
const ENDPOINT = `${ORIGIN}/dev`;
const ROUTE_ROOT = '/internal/browser-identity/v1';
const CURRENT_URL = `${ENDPOINT}${ROUTE_ROOT}/sessions/current`;
const NOW = Date.UTC(2026, 10, 18);
const BODY_HEADERS = Object.freeze({ 'content-type': 'application/json' } as const);
const HANDLE = 'h'.repeat(43);
const CURRENT_BODY = JSON.stringify({ handle: HANDLE });
const ACCESS_KEY_ID = 'ASIAEXAMPLEONLY12345';
const SESSION_TOKEN = 'generated-session-token';
const PAYLOAD_SHA256 = '778c0aa747a31ba9b3996c35c7a465e9f6a35cced0ddb0f834968916373f77e9';
const SIGNATURE = '0a7b562cbd17b5c62b307d335233bff7c05ff0a1d6e6c3a4a01cc87c6bb74b01';

function issued(): TemporaryCredentials {
  return {
    accessKeyId: ACCESS_KEY_ID,
    secretAccessKey: 'generated-signing-secret',
    sessionToken: SESSION_TOKEN,
    expiration: new Date(NOW + 600_000),
  };
}

function fixture() {
  const credentials = mock.fn(async (_signal: AbortSignal) => issued());
  const options: SignerOptions = {
    approvedEndpoint: ENDPOINT, region: REGION, restApiId: REST_API_ID,
    vpcEndpointId: VPC_ENDPOINT_ID, credentials, now: () => NOW,
  };
  return { credentials, options };
}

function currentInput(): PrivateBrowserSignInput {
  return Object.freeze({
    url: CURRENT_URL, method: 'POST', body: CURRENT_BODY, headers: BODY_HEADERS,
  });
}

function closedFailure(code: string) {
  return (error: unknown) => {
    assert.ok(error instanceof IdentityFailure);
    assert.equal(error.code, code);
    assert.equal(error.status, 503);
    assert.equal(error.retryable, false);
    assert.equal(error.message, code);
    assert.equal(error.cause, undefined);
    return true;
  };
}

const UNAVAILABLE = closedFailure('PRIVATE_SIGNING_UNAVAILABLE');

it('signs the exact private current-session bytes through the client using the offline golden', async () => {
  const f = fixture();
  const sign = createPrivateGatewaySigner(f.options);
  const ended = { handle: HANDLE, csrfToken: 'a'.repeat(64), ended: true, generation: 1 };
  const fetch = mock.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    assert.equal(request.url, CURRENT_URL);
    assert.equal(request.method, 'POST');
    assert.equal(await request.text(), CURRENT_BODY);
    assert.deepEqual(Object.fromEntries(request.headers), {
      authorization: `AWS4-HMAC-SHA256 Credential=${ACCESS_KEY_ID}/20261118/us-east-2/execute-api/aws4_request, `
        + 'SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date;x-amz-security-token, '
        + `Signature=${SIGNATURE}`,
      'content-type': 'application/json',
      'x-amz-content-sha256': PAYLOAD_SHA256,
      'x-amz-date': '20261118T000000Z',
      'x-amz-security-token': SESSION_TOKEN,
    });
    return Response.json(ended, { headers: { 'Cache-Control': 'no-store' } });
  });
  const ports = createPrivateBrowserPorts({ approvedEndpoint: ENDPOINT, sign, fetch });

  assert.deepEqual(await ports.sessions.current(HANDLE), ended);
  assert.equal(f.credentials.mock.callCount(), 1);
  assert.equal(fetch.mock.callCount(), 1);
});

it('admits exactly the five pilot operation paths with the configured dev scope', async () => {
  const f = fixture();
  const sign = createPrivateGatewaySigner(f.options);
  const signal = new AbortController().signal;
  const flow = { state: 's'.repeat(43), nonce: 'n'.repeat(43), browserDigest: 'b'.repeat(64),
    verifier: 'v'.repeat(43), expiresAt: NOW + 600_000, returnPath: '/', kind: 'signin',
    sessionHandle: null, subject: null, sessionId: null, generation: null, selectionTarget: null,
    selectionIntent: null };
  const bodies = {
    'flows/create': { flow },
    'flows/consume': { state: `${flow.state}.${flow.nonce}`, browser: flow.browserDigest },
    'sessions/admit': { credentials: { accessToken: 'generated-access', refreshToken: 'generated-refresh',
      userId: 'generated-user' } },
    'sessions/current': { handle: HANDLE },
    'sessions/end': { handle: HANDLE, generation: 0 },
  };
  for (const [path, fields] of Object.entries(bodies)) {
    const signed = await sign(Object.freeze({
      ...currentInput(), url: `${ENDPOINT}${ROUTE_ROOT}/${path}`, body: JSON.stringify(fields),
    }), signal);
    assert.match(signed.authorization, /\/20261118\/us-east-2\/execute-api\/aws4_request,/);
    assert.equal(signed['x-amz-security-token'], SESSION_TOKEN);
  }
  assert.equal(f.credentials.mock.callCount(), 5);
});

testCases([
  ['wrong host', { url: CURRENT_URL.replace(REST_API_ID, 'v9w8x7y6z5') }],
  ['missing stage', { url: `${ORIGIN}${ROUTE_ROOT}/sessions/current` }],
  ['wrong stage', { url: CURRENT_URL.replace('/dev/', '/test/') }],
  ['wrong method', { method: 'GET' }],
  ['query', { url: `${CURRENT_URL}?generated=1` }],
  ['encoded stage', { url: CURRENT_URL.replace('/dev/', '/%64ev/') }],
  ['path alias', { url: `${CURRENT_URL}/` }],
  ['refresh', { url: `${ENDPOINT}${ROUTE_ROOT}/sessions/refresh` }],
] as const)('rejects %s before resolving credentials', async (_name, override) => {
  const f = fixture();
  const sign = createPrivateGatewaySigner(f.options);
  const input = Object.freeze({ ...currentInput(), ...override }) as PrivateBrowserSignInput;
  await assert.rejects(sign(input, new AbortController().signal), UNAVAILABLE);
  assert.equal(f.credentials.mock.callCount(), 0);
});

testCases([
  ['wrong configured Region', { region: 'us-east-1' }],
  ['unmatched REST API', { restApiId: 'v9w8x7y6z5' }],
  ['unmatched VPCE', { vpcEndpointId: 'vpce-1123456789abcdef0' }],
  ['configured non-stage origin', { approvedEndpoint: ORIGIN }],
  ['non-HTTPS', { approvedEndpoint: ENDPOINT.replace('https:', 'http:') }],
  ['noncanonical stage', { approvedEndpoint: `${ORIGIN}/%64ev` }],
  ['query in endpoint', { approvedEndpoint: `${ENDPOINT}?generated=1` }],
  ['userinfo in endpoint', { approvedEndpoint: ENDPOINT.replace('https://', 'https://fixture@') }],
] as const)('refuses malformed startup binding %s', (_name, override) => {
  const f = fixture();
  assert.throws(
    () => createPrivateGatewaySigner({ ...f.options, ...override }),
    closedFailure('IDENTITY_CONFIGURATION_INVALID'),
  );
  assert.equal(f.credentials.mock.callCount(), 0);
});

testCases([
  ['empty temporary token', { sessionToken: '' }],
  ['missing secret', { secretAccessKey: '' }],
  ['invalid expiration', { expiration: new Date(Number.NaN) }],
  ['expired snapshot', { expiration: new Date(NOW - 1) }],
  ['exact expiration', { expiration: new Date(NOW) }],
] as const)('refuses %s from the injected credential provider', async (_name, override) => {
  const f = fixture();
  f.credentials.mock.mockImplementation(async () => ({ ...issued(), ...override }));
  const sign = createPrivateGatewaySigner(f.options);

  await assert.rejects(sign(currentInput(), new AbortController().signal), UNAVAILABLE);
  assert.equal(f.credentials.mock.callCount(), 1);
});

it('refuses a credential provider failure without fallback or raw details', async () => {
  const f = fixture();
  f.credentials.mock.mockImplementation(async () => {
    throw new Error('GENERATED_RAW_PROVIDER_DETAIL');
  });
  const sign = createPrivateGatewaySigner(f.options);

  await assert.rejects(sign(currentInput(), new AbortController().signal), UNAVAILABLE);
  assert.equal(f.credentials.mock.callCount(), 1);
});

it('refuses an already aborted request before invoking the credential provider', async () => {
  const f = fixture();
  const controller = new AbortController();
  controller.abort();
  const sign = createPrivateGatewaySigner(f.options);

  await assert.rejects(sign(currentInput(), controller.signal), UNAVAILABLE);
  assert.equal(f.credentials.mock.callCount(), 0);
});

it('rechecks cancellation and temporary credential expiry after the provider returns', async () => {
  const f = fixture();
  const controller = new AbortController();
  const sign = createPrivateGatewaySigner(f.options);
  f.credentials.mock.mockImplementationOnce(async (signal) => {
    assert.equal(signal, controller.signal);
    controller.abort();
    return issued();
  }, 0);
  await assert.rejects(sign(currentInput(), controller.signal), UNAVAILABLE);

  let now = NOW;
  const credentials = issued();
  const expiringSign = createPrivateGatewaySigner({ ...f.options, now: () => now });
  f.credentials.mock.mockImplementationOnce(async () => {
    now = credentials.expiration.getTime();
    return credentials;
  }, 1);
  await assert.rejects(expiringSign(currentInput(), new AbortController().signal), UNAVAILABLE);
  assert.equal(f.credentials.mock.callCount(), 2);
});
