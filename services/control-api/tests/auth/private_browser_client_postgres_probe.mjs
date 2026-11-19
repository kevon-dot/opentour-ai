// Direct fixture-only Node client for the generated private TLS PostgreSQL test.
import assert from 'node:assert/strict';
import { createHash, createHmac } from 'node:crypto';
import { lookup } from 'node:dns';
import { readFileSync } from 'node:fs';
import { request as requestHttps } from 'node:https';
import { Socket } from 'node:net';
import { join } from 'node:path';
import { connect as connectTls } from 'node:tls';
import { pathToFileURL } from 'node:url';

const [compiled] = process.argv.slice(2);
const config = JSON.parse(readFileSync(0, 'utf8'));
const moduleURL = path => pathToFileURL(join(compiled, path)).href;
const { configureSignedPrivateAccountEntry } = await import(moduleURL('features/account-entry/server.js'));
const { GET: signinGET } = await import(moduleURL('app/auth/signin/route.js'));
const { GET: callbackGET } = await import(moduleURL('app/auth/callback/route.js'));

assert.equal(
  config.endpoint,
  'https://fixtureapi-vpce-01234567.execute-api.us-east-2.amazonaws.com/dev',
);
assert.ok(Number.isInteger(config.local_port) && config.local_port > 0 && config.local_port <= 65535);
const endpoint = new URL(config.endpoint);
const tls = {
  ca: readFileSync(config.certificate_authority),
  cert: readFileSync(config.stage_certificate),
  key: readFileSync(config.stage_private_key),
  rejectUnauthorized: true,
};
const allowedSocket = { ...tls, host: '127.0.0.1', port: config.local_port };
const denied = { code: 'OPENTOUR_NETWORK_DENIED' };
assert.throws(() => lookup(endpoint.hostname, () => {}), denied);
assert.throws(() => new Socket().connect(allowedSocket), denied);
assert.throws(() => connectTls({ ...allowedSocket, host: endpoint.hostname }), denied);
assert.throws(() => connectTls({ ...allowedSocket, port: config.local_port % 65535 + 1 }), denied);
assert.throws(() => connectTls({ ...allowedSocket, rejectUnauthorized: false }), denied);
await assert.rejects(fetch('https://example.invalid'), denied);
const base = '/dev/internal/browser-identity/v1';
const paths = ['/flows/create', '/flows/consume', '/sessions/admit', '/sessions/current', '/sessions/end'];
const accessKeyId = 'fixture-access-id';
const signingSecret = 'generated-fixture-signing-secret';
const sessionToken = 'generated-fixture-session-token';
const signingDate = new Date(config.now).toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z');
const signedHeaderNames = 'content-type;host;x-amz-content-sha256;x-amz-date;x-amz-security-token';
const credentialScope = `${signingDate.slice(0, 8)}/${config.region}/execute-api/aws4_request`;
const authorizationPrefix = `AWS4-HMAC-SHA256 Credential=${accessKeyId}/${credentialScope}, `
  + `SignedHeaders=${signedHeaderNames}, Signature=`;
const exercised = new Set();
let latestSession;
let credentialSnapshots = 0;
let forwarded = 0;

function expectedSignature(pathname, headers, payloadHash) {
  const signedValues = { ...headers, host: endpoint.host };
  const canonicalHeaders = signedHeaderNames.split(';')
    .map(name => `${name}:${signedValues[name]}\n`).join('');
  const canonical = ['POST', pathname, '', canonicalHeaders, signedHeaderNames, payloadHash].join('\n');
  const toSign = ['AWS4-HMAC-SHA256', signingDate, credentialScope,
    createHash('sha256').update(canonical).digest('hex')].join('\n');
  let key = Buffer.from('AWS4' + signingSecret);
  for (const component of credentialScope.split('/')) {
    key = createHmac('sha256', key).update(component).digest();
  }
  return createHmac('sha256', key).update(toSign).digest('hex');
}

async function fetchPrivate(input, init) {
  const request = new Request(input, init);
  assert.equal(request.method, 'POST');
  assert.equal(credentialSnapshots, forwarded + 1);
  const url = new URL(request.url);
  assert.equal(url.origin, endpoint.origin);
  assert.equal(url.search, '');
  assert.equal(url.hash, '');
  const path = paths.find(candidate => url.pathname === base + candidate);
  assert.ok(path);
  const body = Buffer.from(await request.arrayBuffer());
  const payloadHash = createHash('sha256').update(body).digest('hex');
  const headers = Object.fromEntries(request.headers);
  assert.deepEqual(Object.keys(headers).sort(), [
    'authorization', 'content-type', 'x-amz-content-sha256', 'x-amz-date', 'x-amz-security-token',
  ]);
  assert.equal(headers['content-type'], 'application/json');
  assert.equal(headers['x-amz-date'], signingDate);
  assert.equal(headers['x-amz-security-token'], sessionToken);
  assert.equal(headers['x-amz-content-sha256'], payloadHash);
  assert.equal(headers.authorization, authorizationPrefix + expectedSignature(url.pathname, headers, payloadHash));

  const upstream = await new Promise((resolve, reject) => {
    const transport = requestHttps({
      ...tls,
      hostname: '127.0.0.1',
      port: config.local_port,
      servername: endpoint.hostname,
      method: request.method,
      path: url.pathname,
      headers: {
        ...headers,
        host: endpoint.host,
        'content-length': body.length,
      },
      signal: request.signal,
      agent: false,
    }, resolve);
    transport.once('error', reject);
    transport.end(body);
  });
  forwarded++;
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
const workosApiKey = 'loopback-fixture';
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
  const startup = {
    profile,
    workosApiKey,
    now: () => config.now,
    privateIdentity: {
      approvedEndpoint: config.endpoint,
      region: config.region,
      restApiId: config.rest_api_id,
      vpcEndpointId: config.vpc_endpoint_id,
      fetch: fetchPrivate,
      async credentials(signal) {
        assert.ok(signal instanceof AbortSignal && !signal.aborted);
        credentialSnapshots++;
        return { accessKeyId, secretAccessKey: signingSecret, sessionToken,
          expiration: new Date(config.now + 600_000) };
      },
    },
  };
  assert.throws(() => configureSignedPrivateAccountEntry({
    ...startup, privateIdentity: { ...startup.privateIdentity, restApiId: 'otherfixture' },
  }), { code: 'IDENTITY_CONFIGURATION_INVALID', status: 503 });
  for (const result of [
    await signinGET(new Request(profile.origin + '/auth/signin')),
    await callbackGET(new Request(profile.callbackURI)),
  ]) {
    assert.equal(result.status, 503);
    assert.equal((await result.json()).code, 'IDENTITY_NOT_CONFIGURED');
  }
  const adapter = configureSignedPrivateAccountEntry(startup);
  assert.throws(() => configureSignedPrivateAccountEntry(startup), {
    code: 'IDENTITY_ALREADY_CONFIGURED', status: 503,
  });
  assert.equal(credentialSnapshots, 0);
  assert.equal(forwarded, 0);
  assert.equal(exchanges, 0);
  assert.equal(jwksRequests, 0);

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
  assert.equal(credentialSnapshots, forwarded);
  assert.deepEqual([...exercised].sort(), [...paths].sort());
} finally {
  globalThis.fetch = originalFetch;
}

console.log(JSON.stringify({
  account_entry_over_private_mtls: 'PASS',
  pinned_signer: 'PASS',
  provider_acceptance: 'NOT_RUN',
  mode: config.mode,
}));
