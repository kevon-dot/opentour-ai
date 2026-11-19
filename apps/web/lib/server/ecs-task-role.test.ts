import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import http from 'node:http';
import { Readable } from 'node:stream';
import { it, mock } from 'node:test';
import { testCases } from '../../tests/web-unit-cases.js';

mock.module('server-only', { namedExports: {} });
const { IdentityFailure } = await import('./authkit-adapter.js');
const { createEcsTaskCredentialProvider, fetchEcsTaskCredentials } = await import('./ecs-task-role.js');

const RELATIVE_URI = '/v2/credentials/01234567-89ab-cdef-0123-456789abcdef';
const ENDPOINT = `http://169.254.170.2${RELATIVE_URI}`;
const ROLE = 'arn:aws:iam::000000000000:role/fixture/bff';
const NOW = Date.UTC(2026, 10, 18);
const SNAPSHOT = Object.freeze({
  RoleArn: ROLE, AccessKeyId: 'fixture-access', SecretAccessKey: 'fixture-secret',
  Token: 'fixture-token', Expiration: new Date(NOW + 60_000).toISOString(),
});
const SIGNAL = new AbortController().signal;

function options(fetch: typeof globalThis.fetch) {
  return { relativeUri: RELATIVE_URI, expectedRoleArn: ROLE, fetch, now: () => NOW };
}

function safeFailure(code: string) {
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

const INVALID = safeFailure('IDENTITY_CONFIGURATION_INVALID');
const UNAVAILABLE = safeFailure('ECS_TASK_CREDENTIALS_UNAVAILABLE');

it('resolves a fresh snapshot per signing request through the fixed ECS endpoint', async () => {
  const fetch = mock.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    assert.equal(input, ENDPOINT);
    assert.ok(init?.signal instanceof AbortSignal);
    assert.equal(init.method, 'GET');
    assert.equal(init.redirect, 'error');
    assert.equal(init.cache, 'no-store');
    assert.equal(init.credentials, 'omit');
    assert.equal(init.referrerPolicy, 'no-referrer');
    assert.equal(init.body, undefined);
    assert.deepEqual(Object.fromEntries(new Headers(init.headers)), { accept: 'application/json' });
    return Response.json({ ...SNAPSHOT, Token: 'fixture-refreshed' });
  });
  fetch.mock.mockImplementationOnce(async () => Response.json(SNAPSHOT), 0);
  const credentials = createEcsTaskCredentialProvider(options(fetch));

  assert.equal(fetch.mock.callCount(), 0);
  const first = await credentials(SIGNAL);
  assert.deepEqual(first, {
    accessKeyId: SNAPSHOT.AccessKeyId, secretAccessKey: SNAPSHOT.SecretAccessKey,
    sessionToken: SNAPSHOT.Token, expiration: new Date(SNAPSHOT.Expiration),
  });
  assert.deepEqual(await credentials(SIGNAL), { ...first, sessionToken: 'fixture-refreshed' });
  assert.equal(fetch.mock.callCount(), 2);
});

testCases([
  undefined, '', ENDPOINT, `/${RELATIVE_URI}`, `${RELATIVE_URI}?fixture=1`, `${RELATIVE_URI}\n`,
  `${RELATIVE_URI}#fixture`, `${RELATIVE_URI}/`, RELATIVE_URI.replace('/v2/', '/v1/'),
  RELATIVE_URI.replace('/v2/', '/%762/'),
])('refuses invalid ECS relative URI %j before transport', relativeUri => {
  const fetch = mock.fn(async () => Response.json(SNAPSHOT));
  assert.throws(() => createEcsTaskCredentialProvider({ ...options(fetch), relativeUri }), INVALID);
  assert.equal(fetch.mock.callCount(), 0);
});

it('requires an exact configured IAM role ARN and explicit transport', () => {
  const fetch = mock.fn(async () => Response.json(SNAPSHOT));
  for (const expectedRoleArn of ['', `${ROLE}\n`, ROLE.replace('iam::', 'sts::')]) {
    assert.throws(() => createEcsTaskCredentialProvider({ ...options(fetch), expectedRoleArn }), INVALID);
  }
  assert.throws(() => createEcsTaskCredentialProvider({
    ...options(fetch), fetch: undefined as unknown as typeof fetch,
  }), INVALID);
  assert.equal(fetch.mock.callCount(), 0);
});

testCases([
  ['foreign role', { RoleArn: `${ROLE}-foreign` }],
  ['missing key', { AccessKeyId: undefined }],
  ['malformed key', { AccessKeyId: 'fixture:access' }],
  ['empty secret', { SecretAccessKey: '' }],
  ['oversized secret', { SecretAccessKey: 'x'.repeat(1025) }],
  ['empty token', { Token: '' }],
  ['header-breaking token', { Token: 'fixture\ntoken' }],
  ['oversized token', { Token: 'x'.repeat(16_385) }],
  ['expired timestamp', { Expiration: new Date(NOW - 1).toISOString() }],
  ['exact expiry', { Expiration: new Date(NOW).toISOString() }],
  ['non-timestamp', { Expiration: '2099' }],
  ['normalized invalid date', { Expiration: '2099-02-31T00:00:00Z' }],
  ['unknown credential field', { Code: 'Success' }],
] as const)('rejects an ECS snapshot with %s without fallback', async (_label, override) => {
  const fetch = mock.fn(async () => Response.json({ ...SNAPSHOT, ...override }));
  await assert.rejects(createEcsTaskCredentialProvider(options(fetch))(SIGNAL), UNAVAILABLE);
  assert.equal(fetch.mock.callCount(), 1);
});

testCases([
  ['non-200 status', () => Response.json(SNAPSHOT, { status: 503 })],
  ['redirect', () => new Response(null, { status: 302, headers: { Location: 'https://fixture.invalid/' } })],
  ['unexpected final URL', () => Object.defineProperty(Response.json(SNAPSHOT), 'url', {
    value: 'http://fixture.invalid/',
  })],
  ['non-JSON media type', () => new Response(JSON.stringify(SNAPSHOT))],
  ['compressed response', () => Response.json(SNAPSHOT, { headers: { 'Content-Encoding': 'gzip' } })],
  ['malformed UTF-8', () => new Response(new Uint8Array([255]), {
    headers: { 'Content-Type': 'application/json' },
  })],
  ['response above the byte limit', () => new Response(' '.repeat(24_577) + JSON.stringify(SNAPSHOT), {
    headers: { 'Content-Type': 'application/json' },
  })],
] as const)('rejects credential HTTP response with %s', async (_label, response) => {
  const fetch = mock.fn(async () => response());
  await assert.rejects(createEcsTaskCredentialProvider(options(fetch))(SIGNAL), UNAVAILABLE);
  assert.equal(fetch.mock.callCount(), 1);
});

it('refuses a cancelled request and forwards cancellation to a pending transport', async () => {
  let aborted = false;
  const fetch = mock.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    assert.ok(init?.signal);
    return new Promise<Response>(resolve => init.signal!.addEventListener('abort', () => {
      aborted = true;
      resolve(Response.json(SNAPSHOT));
    }, { once: true }));
  });
  const credentials = createEcsTaskCredentialProvider(options(fetch));
  await assert.rejects(credentials(AbortSignal.abort()), UNAVAILABLE);
  assert.equal(fetch.mock.callCount(), 0);

  const controller = new AbortController();
  const pending = credentials(controller.signal);
  controller.abort();
  await assert.rejects(pending, UNAVAILABLE);
  assert.equal(aborted, true);
  assert.equal(fetch.mock.callCount(), 1);
});

it('uses its own direct HTTP agent and refuses arbitrary destinations', async t => {
  const request = t.mock.method(http, 'request', (...args: unknown[]): http.ClientRequest => {
    const [target, requestOptions, accept] = args as [string, http.RequestOptions,
      (message: http.IncomingMessage) => void];
    assert.equal(target, ENDPOINT);
    assert.equal(requestOptions.method, 'GET');
    assert.ok(requestOptions.signal instanceof AbortSignal);
    assert.ok(requestOptions.agent instanceof http.Agent);
    assert.notEqual(requestOptions.agent, http.globalAgent);
    assert.deepEqual(requestOptions.headers, { Accept: 'application/json' });
    return Object.assign(new EventEmitter(), { end: () => {
      accept(Object.assign(Readable.from([Buffer.from(JSON.stringify(SNAPSHOT))]), {
        statusCode: 200, headers: { 'content-type': 'application/json; charset=utf-8' },
      }) as http.IncomingMessage);
    } }) as http.ClientRequest;
  });
  await assert.rejects(fetchEcsTaskCredentials('http://fixture.invalid' + RELATIVE_URI, {
    method: 'GET', redirect: 'error', signal: SIGNAL,
  }), UNAVAILABLE);
  assert.equal(request.mock.callCount(), 0);

  const credentials = createEcsTaskCredentialProvider(options(fetchEcsTaskCredentials));
  assert.equal((await credentials(SIGNAL)).sessionToken, SNAPSHOT.Token);
  assert.equal(request.mock.callCount(), 1);
});
