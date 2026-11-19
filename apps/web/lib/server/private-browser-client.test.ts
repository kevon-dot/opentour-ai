/** Generated private transport fixtures. No deployed signing or provider calls. */
import assert from 'node:assert/strict';
import { it, mock } from 'node:test';
import { testCases } from '../../tests/web-unit-cases.js';
import type { ActiveServerSession, AuthTransaction, Claims, Credentials, EndedServerSession } from './authkit-adapter.js';

mock.module('server-only', { namedExports: {} });
const { IdentityFailure } = await import('./authkit-adapter.js');
const { createPrivateBrowserPorts } = await import('./private-browser-client.js');

type Options = Parameters<typeof createPrivateBrowserPorts>[0];
type SignRequest = Parameters<Options['sign']>[0];
const ENDPOINT = 'https://fixture.example.invalid/dev';
const BASE_PATH = '/internal/browser-identity/v1';
const HANDLE = 'h'.repeat(43);
const CSRF = 'a'.repeat(64);
const CHECKED = '2026-11-18T00:00:00.123456Z';
const CREDENTIALS: Credentials = {
  accessToken: 'GENERATED_ACCESS_SENTINEL',
  refreshToken: 'GENERATED_REFRESH_SENTINEL',
  userId: 'generated_user',
};
const CLAIMS: Claims = {
  issuer: 'https://issuer.example.invalid/',
  subject: CREDENTIALS.userId,
  sessionId: 'generated_session',
  organizationId: null,
  authenticationTime: CHECKED,
  expiresAt: Date.UTC(2026, 10, 18, 0, 10),
};
const ACTIVE: ActiveServerSession = {
  handle: HANDLE, csrfToken: CSRF, ended: false, reconciliationRequired: false,
  credentials: CREDENTIALS,
  identity: {
    actor: { kind: 'human', subject: CLAIMS.subject, issuer: CLAIMS.issuer },
    session_id: CLAIMS.sessionId, session_generation: 0, identity_checked_at: CHECKED,
    authentication_time: CLAIMS.authenticationTime, assurance_level: 'unknown',
  },
};
const ENDED: EndedServerSession = { handle: HANDLE, csrfToken: CSRF, ended: true, generation: 1 };
const FLOW: AuthTransaction = {
  state: 's'.repeat(43), nonce: 'n'.repeat(43), browserDigest: 'b'.repeat(64),
  verifier: 'v'.repeat(43), expiresAt: CLAIMS.expiresAt, returnPath: '/setup',
  kind: 'signin', sessionHandle: null, subject: null, sessionId: null, generation: null,
  selectionTarget: null, selectionIntent: null,
};
const SIGNED = {
  authorization: 'AWS4-HMAC-SHA256 Credential=GENERATED_SIGNATURE_SENTINEL',
  'x-amz-date': '20261118T000000Z',
  'x-amz-security-token': 'GENERATED_SESSION_TOKEN_SENTINEL',
};

function json(value: unknown, status = 200): Response {
  return Response.json(value, { status, headers: { 'Cache-Control': 'no-store' } });
}

function fixture(reply: (index: number) => Response | Promise<Response> = () => json(null)) {
  let calls = 0;
  const sign = mock.fn(async (_request: SignRequest, _signal: AbortSignal) => ({ ...SIGNED }));
  const fetch = mock.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => reply(calls++));
  return { sign, fetch, ports: createPrivateBrowserPorts({ approvedEndpoint: ENDPOINT, sign, fetch }) };
}

function safeRefusal(code: string, status: number) {
  return (error: unknown) => {
    assert.ok(error instanceof IdentityFailure);
    assert.equal(error.code, code);
    assert.equal(error.status, status);
    assert.equal(error.message, code);
    assert.equal(error.retryable, false);
    assert.equal(error.cause, undefined);
    return true;
  };
}

it('signs and sends only the five exact dev requests and closed payloads', async () => {
  const receipt = { sessionId: CLAIMS.sessionId, generation: ENDED.generation };
  const replies = [null, FLOW, ACTIVE, ENDED, receipt];
  const f = fixture(index => json(replies[index]));
  const state = `${FLOW.state}.${FLOW.nonce}`;

  assert.equal(await f.ports.transactions.create(FLOW), undefined);
  assert.deepEqual(await f.ports.transactions.consume(state, FLOW.browserDigest, 42), FLOW);
  assert.deepEqual(await f.ports.sessions.admit(CREDENTIALS, CLAIMS), ACTIVE);
  assert.deepEqual(await f.ports.sessions.current(HANDLE), ENDED);
  assert.deepEqual(await f.ports.sessions.end(HANDLE, 0), receipt);

  const expected = [
    ['flows/create', { flow: FLOW }],
    ['flows/consume', { state, browser: FLOW.browserDigest }],
    ['sessions/admit', { credentials: CREDENTIALS }],
    ['sessions/current', { handle: HANDLE }],
    ['sessions/end', { handle: HANDLE, generation: 0 }],
  ] as const;
  assert.equal(f.sign.mock.callCount(), expected.length);
  assert.equal(f.fetch.mock.callCount(), expected.length);
  for (const [index, [suffix, payload]] of expected.entries()) {
    const url = `${ENDPOINT}${BASE_PATH}/${suffix}`;
    const [signed, signSignal] = f.sign.mock.calls[index]!.arguments;
    assert.ok(Object.isFrozen(signed));
    assert.ok(Object.isFrozen(signed.headers));
    assert.deepEqual(signed, { url, method: 'POST', body: JSON.stringify(payload),
      headers: { 'content-type': 'application/json' } });
    assert.ok(signSignal instanceof AbortSignal);
    const [target, init] = f.fetch.mock.calls[index]!.arguments;
    assert.equal(init?.signal, signSignal);
    const sent = new Request(target, init);
    assert.equal(sent.url, url);
    assert.equal(sent.method, 'POST');
    assert.equal(sent.cache, 'no-store');
    assert.equal(sent.redirect, 'error');
    assert.equal(sent.credentials, 'omit');
    assert.equal(sent.referrerPolicy, 'no-referrer');
    assert.deepEqual(Object.fromEntries(sent.headers), { 'content-type': 'application/json', ...SIGNED });
    assert.equal(await sent.text(), signed.body);
  }
});

it('refuses extraneous signer headers before transport', async () => {
  const f = fixture();
  f.sign.mock.mockImplementation(async () => ({ ...SIGNED, cookie: 'generated_untrusted_cookie' }));

  await assert.rejects(f.ports.sessions.current(HANDLE), safeRefusal('IDENTITY_STATE_UNAVAILABLE', 503));
  assert.equal(f.sign.mock.callCount(), 1);
  assert.equal(f.fetch.mock.callCount(), 0);
});

it('refuses to send when the signer returns at the monotonic deadline', async (t) => {
  let elapsed = 0;
  t.mock.method(performance, 'now', () => elapsed);
  const f = fixture();
  f.sign.mock.mockImplementation(async () => {
    elapsed = 25_000;
    return { ...SIGNED };
  });

  await assert.rejects(f.ports.transactions.create(FLOW), safeRefusal('IDENTITY_STATE_UNAVAILABLE', 503));
  assert.equal(f.sign.mock.callCount(), 1);
  assert.equal(f.fetch.mock.callCount(), 0);
});

it('compares admission against the credentials and claim coordinates captured before signing', async () => {
  const offered = { ...CREDENTIALS };
  const claims = { ...CLAIMS };
  const f = fixture(() => json(ACTIVE));
  f.sign.mock.mockImplementation(async () => {
    offered.accessToken = 'generated_changed_access';
    claims.sessionId = 'generated_other_session';
    return { ...SIGNED };
  });

  assert.deepEqual(await f.ports.sessions.admit(offered, claims), ACTIVE);
  assert.deepEqual(JSON.parse(f.sign.mock.calls[0]!.arguments[0].body), { credentials: CREDENTIALS });
});

it('preserves the server receipt for an already ended session with a stale request generation', async () => {
  const receipt = { sessionId: CLAIMS.sessionId, generation: 5 };
  const f = fixture(() => json(receipt));
  assert.deepEqual(await f.ports.sessions.end(HANDLE, 0), receipt);
  assert.equal(f.fetch.mock.callCount(), 1);
});

it('keeps refresh, reconciliation and selection disabled without signing or transport', async () => {
  const f = fixture();
  const intent = { organizationId: 'generated_org', expectedGeneration: 0, idempotencyKey: 'generated-key' };
  const target = {
    organizationId: intent.organizationId, identityOrganizationId: 'generated_provider_org',
    identityIssuer: CLAIMS.issuer, membershipId: 'generated_member', authorityRevision: 'r1',
  };
  const disabled = [
    () => f.ports.sessions.replaceCredentials(HANDLE, 0, CREDENTIALS, CLAIMS),
    () => f.ports.sessions.requireReconciliation(HANDLE, 0),
    () => f.ports.selections.authorize(HANDLE, ACTIVE.identity, intent),
    () => f.ports.selections.commit(HANDLE, ACTIVE.identity, intent, target, CLAIMS, 'a'.repeat(64)),
  ];
  for (const call of disabled) await assert.rejects(call, safeRefusal('PRIVATE_OPERATION_DISABLED', 403));
  assert.equal(f.sign.mock.callCount(), 0);
  assert.equal(f.fetch.mock.callCount(), 0);
});

testCases([
  ['consume', 'IDENTITY_STATE_UNAVAILABLE'],
  ['admit', 'SESSION_ADMISSION_UNCONFIRMED'],
  ['end', 'SESSION_END_UNCONFIRMED'],
] as const)('does not retry or reveal a lost %s acknowledgement', async (operation, code) => {
  const f = fixture(() => { throw new Error('GENERATED_RAW_TRANSPORT_DETAIL'); });
  const calls = {
    consume: () => f.ports.transactions.consume(`${FLOW.state}.${FLOW.nonce}`, FLOW.browserDigest, 42),
    admit: () => f.ports.sessions.admit(CREDENTIALS, CLAIMS),
    end: () => f.ports.sessions.end(HANDLE, 0),
  };

  await assert.rejects(calls[operation], safeRefusal(code, 503));
  assert.equal(f.sign.mock.callCount(), 1);
  assert.equal(f.fetch.mock.callCount(), 1);
});

testCases([
  ['broken JSON', () => new Response('{', { headers: {
    'Content-Type': 'application/json', 'Cache-Control': 'no-store',
  } })],
  ['non-JSON media', () => new Response(JSON.stringify(ENDED), { headers: {
    'Content-Type': 'text/plain', 'Cache-Control': 'no-store',
  } })],
  ['missing no-store', () => Response.json(ENDED)],
  ['ended credentials', () => json({ ...ENDED, credentials: CREDENTIALS })],
  ['wrong returned handle', () => json({ ...ENDED, handle: 'x'.repeat(43) })],
] as const)('fails closed on a current response with %s without retrying', async (_name, reply) => {
  const f = fixture(reply);
  await assert.rejects(f.ports.sessions.current(HANDLE), safeRefusal('IDENTITY_STATE_UNAVAILABLE', 503));
  assert.equal(f.sign.mock.callCount(), 1);
  assert.equal(f.fetch.mock.callCount(), 1);
});

it('rejects a request over the encoded byte cap before signing', async () => {
  const f = fixture();
  const unicode = '界'.repeat(16_384);
  const credentials = { ...CREDENTIALS, accessToken: unicode, refreshToken: unicode };
  const body = JSON.stringify({ credentials });
  assert.ok(body.length < 65_536 && Buffer.byteLength(body) > 65_536);

  await assert.rejects(f.ports.sessions.admit(credentials, CLAIMS), safeRefusal('SCHEMA_INVALID', 422));
  assert.equal(f.sign.mock.callCount(), 0);
  assert.equal(f.fetch.mock.callCount(), 0);
});

it('rejects a JSON acknowledgement over the response byte cap even without Content-Length', async () => {
  const raw = ' '.repeat(131_069) + 'null';
  const f = fixture(() => new Response(raw, { headers: {
    'Content-Type': 'application/json', 'Cache-Control': 'no-store',
  } }));
  assert.equal(Buffer.byteLength(raw), 131_073);

  await assert.rejects(f.ports.transactions.create(FLOW), safeRefusal('IDENTITY_STATE_UNAVAILABLE', 503));
  assert.equal(f.sign.mock.callCount(), 1);
  assert.equal(f.fetch.mock.callCount(), 1);
});

testCases([
  'https://fixture.example.invalid/dev?probe=generated',
  'https://generated-user@fixture.example.invalid/dev',
  'https://fixture.example.invalid/test',
  'https://fixture.example.invalid/dev/',
  'https://fixture.example.invalid/dev/dev',
  'https://fixture.example.invalid/%64ev',
  'https://fixture.example.invalid/dev#generated',
  'http://fixture.example.invalid/dev',
])('refuses invalid dev startup binding %s', (approvedEndpoint) => {
  const { sign, fetch } = fixture();
  assert.throws(
    () => createPrivateBrowserPorts({ approvedEndpoint, sign, fetch }),
    safeRefusal('IDENTITY_CONFIGURATION_INVALID', 503),
  );
  assert.equal(sign.mock.callCount(), 0);
  assert.equal(fetch.mock.callCount(), 0);
});
