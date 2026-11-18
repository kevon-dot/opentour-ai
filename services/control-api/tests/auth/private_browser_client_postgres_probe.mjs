// Direct fixture-only Node client for the generated private TLS PostgreSQL test.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { request as requestHttps } from 'node:https';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [compiled] = process.argv.slice(2);
const config = JSON.parse(readFileSync(0, 'utf8'));
const moduleURL = path => pathToFileURL(join(compiled, path)).href;
const { configurePrivateAccountEntry } = await import(moduleURL('features/account-entry/server.js'));
const { GET: signinGET } = await import(moduleURL('app/auth/signin/route.js'));
const { GET: callbackGET } = await import(moduleURL('app/auth/callback/route.js'));

const endpoint = new URL(config.endpoint);
assert.equal(endpoint.hostname, 'browser.fixture.test');
assert.equal(endpoint.pathname, '/dev');
const tls = {
  ca: readFileSync(config.certificate_authority),
  cert: readFileSync(config.stage_certificate),
  key: readFileSync(config.stage_private_key),
  rejectUnauthorized: true,
};
const base = '/dev/internal/browser-identity/v1';
const paths = ['/flows/create', '/flows/consume', '/sessions/admit', '/sessions/current', '/sessions/end'];
const signedHeaders = {
  authorization: 'AWS4-HMAC-SHA256 generated-fixture-signature',
  'x-amz-date': '20261118T000000Z',
  'x-amz-security-token': 'generated-fixture-session-token',
};
const exercised = new Set();
let pendingProof;
let latestSession;

async function sign(request, signal) {
  assert.equal(pendingProof, undefined);
  assert.ok(Object.isFrozen(request));
  assert.ok(signal instanceof AbortSignal);
  assert.equal(request.method, 'POST');
  assert.deepEqual(request.headers, { 'content-type': 'application/json' });
  assert.equal(typeof request.body, 'string');
  assert.ok(JSON.parse(request.body));
  pendingProof = request;
  return signedHeaders;
}

async function fetchPrivate(input, init) {
  const request = new Request(input, init);
  const url = new URL(request.url);
  assert.equal(url.origin, endpoint.origin);
  assert.equal(url.search, '');
  assert.equal(url.hash, '');
  const path = paths.find(candidate => url.pathname === base + candidate);
  assert.ok(path);
  const body = await request.text();
  assert.equal(request.url, pendingProof.url);
  assert.equal(request.method, pendingProof.method);
  assert.equal(body, pendingProof.body);
  assert.equal(request.headers.get('content-type'), 'application/json');
  for (const [name, value] of Object.entries(signedHeaders)) {
    assert.equal(request.headers.get(name), value);
  }
  pendingProof = undefined;

  const upstream = await new Promise((resolve, reject) => {
    const transport = requestHttps({
      ...tls,
      hostname: '127.0.0.1',
      port: endpoint.port,
      servername: endpoint.hostname,
      method: request.method,
      path: url.pathname,
      headers: {
        ...Object.fromEntries(request.headers),
        host: endpoint.host,
        'content-length': Buffer.byteLength(body),
      },
      signal: request.signal,
      agent: false,
    }, resolve);
    transport.once('error', reject);
    transport.end(body);
  });
  const chunks = [];
  let size = 0;
  for await (const chunk of upstream) {
    size += chunk.length;
    assert.ok(size <= 131_072);
    chunks.push(chunk);
  }
  const data = Buffer.concat(chunks, size);
  const contentType = upstream.headers['content-type'];
  const cacheControl = upstream.headers['cache-control'];
  assert.equal(contentType, 'application/json');
  assert.equal(cacheControl, 'no-store');
  if (path === '/sessions/current' && upstream.statusCode === 200) {
    latestSession = JSON.parse(data);
  }
  exercised.add(path);
  return new Response(data, {
    status: upstream.statusCode,
    headers: { 'content-type': contentType, 'cache-control': cacheControl },
  });
}

const profile = {
  origin: 'https://app.example.invalid',
  callbackURI: 'https://app.example.invalid/auth/callback',
  signedOutPath: '/signed-out',
  returnPaths: ['/', '/signed-out'],
  providerOrigins: ['https://api.workos.com'],
  issuer: config.issuer,
  audience: config.audience,
  jwksURL: config.jwks_url,
  clientId: config.client_id,
};
const workosApiKey = 'generated-workos-api-key';
let exchanges = 0;
let jwksRequests = 0;
const originalFetch = globalThis.fetch;
globalThis.fetch = async (input, init) => {
  const request = new Request(input, init);
  if (request.url === profile.jwksURL) {
    assert.equal(request.method, 'GET');
    jwksRequests++;
    return Response.json(config.jwks);
  }
  assert.equal(request.url, 'https://api.workos.com/user_management/authenticate');
  assert.equal(request.method, 'POST');
  const body = await request.json();
  assert.equal(body.code, 'generated');
  assert.equal(body.client_id, profile.clientId);
  assert.equal(body.client_secret, workosApiKey);
  assert.match(body.code_verifier, /^[A-Za-z0-9_-]{43}$/);
  exchanges++;
  return Response.json({
    access_token: config.access_token,
    refresh_token: 'GENERATED_PRIVATE_NODE_REFRESH',
    user: {
      id: config.subject,
      email: 'generated@example.invalid',
      email_verified: true,
      first_name: null,
      last_name: null,
      profile_picture_url: null,
      created_at: new Date(config.now).toISOString(),
      updated_at: new Date(config.now).toISOString(),
    },
  });
};

try {
  const adapter = configurePrivateAccountEntry({
    profile,
    workosApiKey,
    now: () => config.now,
    privateIdentity: { approvedEndpoint: config.endpoint, sign, fetch: fetchPrivate },
  });
  const signin = await signinGET(new Request(profile.origin + '/auth/signin'));
  assert.equal(signin.status, 303);
  const state = new URL(signin.headers.get('location')).searchParams.get('state');
  const flowCookie = signin.headers.getSetCookie().find(value =>
    value.startsWith('opentour-auth-flow=')).split(';')[0];
  const callback = new Request(
    profile.callbackURI + '?state=' + encodeURIComponent(state) + '&code=generated',
    { headers: { cookie: flowCookie } },
  );
  const admitted = await callbackGET(callback);
  assert.equal(admitted.status, 303);
  const cookie = admitted.headers.getSetCookie().find(value =>
    value.startsWith('opentour-session=')).split(';')[0];
  assert.equal((await callbackGET(callback)).status, 401);
  const verified = await adapter.verifiedIdentity(
    new Request(profile.origin, { headers: { cookie } }), 'session_context',
  );
  assert.equal(verified.session_id, config.session_id);
  assert.equal(verified.actor.subject, config.subject);
  assert.equal(verified.session_generation, 0);
  assert.equal(latestSession.ended, false);

  const ended = await adapter.signout(new Request(profile.origin + '/auth/signout', {
    method: 'POST',
    headers: { cookie, origin: profile.origin, 'x-csrf-token': latestSession.csrfToken },
  }));
  assert.equal(ended.status, 303);
  await assert.rejects(
    adapter.verifiedIdentity(new Request(profile.origin, { headers: { cookie } }), 'session_context'),
    { code: 'SESSION_ENDED', status: 401 },
  );
  assert.equal(latestSession.ended, true);
  assert.equal(latestSession.generation, 1);
  assert.equal('credentials' in latestSession, false);
  assert.equal(exchanges, 1);
  assert.equal(jwksRequests, 1);
  assert.equal(pendingProof, undefined);
  assert.deepEqual([...exercised].sort(), [...paths].sort());
} finally {
  globalThis.fetch = originalFetch;
}

console.log(JSON.stringify({
  account_entry_over_private_mtls: 'PASS',
  provider_acceptance: 'NOT_RUN',
  mode: config.mode,
}));
