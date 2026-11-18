// Test-only exported account handlers, real PostgreSQL ports and generated SDK responses.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { pathToFileURL } from 'node:url';
const [compiled, python, bridgePath, configPath] = process.argv.slice(2);
const moduleURL = (path) => pathToFileURL(join(compiled, path)).href;
const require = createRequire(moduleURL('package.json'));
const { exportJWK, generateKeyPair, SignJWT } = await import(pathToFileURL(require.resolve('jose')).href);
const { IdentityFailure } = await import(moduleURL('lib/server/authkit-adapter.js'));
const { configureAccountEntry } = await import(moduleURL('features/account-entry/server.js'));
const { GET: signinGET } = await import(moduleURL('app/auth/signin/route.js'));
const { GET: callbackGET } = await import(moduleURL('app/auth/callback/route.js'));
const config = JSON.parse(readFileSync(configPath, 'utf8'));
const profile = { origin: 'https://app.example.invalid', callbackURI: 'https://app.example.invalid/auth/callback',
  signedOutPath: '/signed-out', returnPaths: ['/', '/signed-out'], providerOrigins: ['https://api.workos.com'],
  issuer: config.issuer, audience: 'https://api.example.invalid', jwksURL: 'https://api.workos.com/sso/jwks/client_fixture',
  clientId: 'client_fixture' };
let exchanges = 0;
let jwksRequests = 0;
let admissionUnknown = false;
let endUnknown = false;
let nextSid = config.sid;
const { publicKey, privateKey } = await generateKeyPair('RS256');
const jwk = { ...await exportJWK(publicKey), kid: 'generated', alg: 'RS256', use: 'sig' };
writeFileSync(join(dirname(configPath), 'generated-jwks.json'), JSON.stringify({ keys: [jwk] }), {
  flag: 'wx', mode: 0o600,
});

// The bounded child exists only for this generated fixture. Each request consumes one response.
const bridge = spawn(python, [bridgePath, configPath], {
  stdio: ['pipe', 'pipe', 'ignore'], timeout: 30_000, killSignal: 'SIGKILL',
});
const exit = new Promise(resolve => bridge.once('close', (code, signal) => resolve({ code, signal })));
let launchFailed = false;
bridge.on('error', () => { launchFailed = true; });
bridge.stdin.on('error', () => bridge.kill('SIGKILL'));
const responses = bridge.stdout[Symbol.asyncIterator]();
let requestPending = false;

async function withinDeadline(pending) {
  let timer;
  try {
    return await Promise.race([pending, new Promise((_, reject) => {
      timer = setTimeout(() => {
        bridge.kill('SIGKILL');
        reject(new Error('Generated Python bridge timed out'));
      }, 5_000);
    })]);
  } finally {
    clearTimeout(timer);
  }
}

async function readFrame() {
  const chunks = [];
  let size = 0;
  while (true) {
    const { done, value: chunk } = await responses.next();
    if (done) throw new Error('Generated Python bridge closed before its response');
    size += chunk.length;
    const newline = chunk.indexOf(10);
    if (size > 131_072 || (newline !== -1 && newline !== chunk.length - 1)) {
      bridge.kill('SIGKILL');
      throw new Error('Generated Python bridge returned an invalid frame');
    }
    chunks.push(chunk);
    if (newline !== -1) return JSON.parse(Buffer.concat(chunks, size).toString('utf8'));
  }
}

async function port(action, data = {}) {
  const frame = Buffer.from(JSON.stringify({ action, ...data }) + '\n');
  if (requestPending || frame.length > 65_536) throw new Error('Invalid generated Python request');
  requestPending = true;
  try {
    bridge.stdin.write(frame);
    const result = await withinDeadline(readFrame());
    if (result.error) throw new IdentityFailure(result.error.code, result.error.status);
    return result.value;
  } finally {
    requestPending = false;
  }
}

async function closeBridge() {
  bridge.stdin.end();
  const { code, signal } = await withinDeadline(exit);
  if (launchFailed || code !== 0 || signal !== null) {
    throw new Error('Generated Python bridge did not close cleanly');
  }
}

function issueToken(sessionId, overrides = {}) {
  return new SignJWT({ iss: profile.issuer, aud: profile.audience, sub: 'user_a', sid: sessionId,
    iat: config.now/1000-60, exp: config.now/1000+600, auth_time: config.now/1000-120, ...overrides })
    .setProtectedHeader({ alg: 'RS256', kid: jwk.kid }).sign(privateKey);
}
const workosApiKey = 'generated-api-key';
async function generatedFetch(input, init) {
  const request = new Request(input, init);
  if (request.url === profile.jwksURL) {
    assert.equal(request.method, 'GET');
    jwksRequests++;
    return Response.json({ keys: [jwk] });
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
    access_token: await issueToken(nextSid),
    refresh_token: 'GENERATED_REFRESH_SENTINEL',
    user: { id: 'user_a', email: 'generated@example.invalid', email_verified: true,
      first_name: null, last_name: null, profile_picture_url: null,
      created_at: new Date(config.now).toISOString(), updated_at: new Date(config.now).toISOString() },
  });
}
const sessions = {
  admit: async (credentials) => port(admissionUnknown ? 'admit_unknown' : 'admit', { credentials }),
  current: async (handle) => port('current', { handle }),
  replaceCredentials: async (handle, generation, credentials) =>
    port('replace', { handle, generation, credentials }),
  end: async (handle, generation) => port(endUnknown ? 'end_unknown' : 'end', { handle, generation }),
  requireReconciliation: async (handle, generation) => port('reconcile', { handle, generation }),
};
const startup = { profile, workosApiKey, sessions, now: () => config.now,
  transactions: {
    create: async (flow) => port('create_flow', { flow }),
    consume: async (state, browser) => port('consume_flow', { state, browser }),
  },
  selections: {
    authorize: async () => { throw new Error('Selection belongs to172 and is not invoked by signin'); },
    commit: async () => { throw new Error('Selection belongs to172 and is not invoked by signin'); },
  } };
const signinURL = profile.origin + '/auth/signin';
async function signin() {
  const response = await signinGET(new Request(signinURL));
  assert.equal(response.status, 303);
  const state = new URL(response.headers.get('location')).searchParams.get('state');
  const cookie = response.headers.get('set-cookie').split(';')[0];
  return new Request(`${profile.callbackURI}?state=${state}&code=generated`, { headers: { cookie } });
}
async function runProbe() {
  for (const response of [
    await signinGET(new Request(signinURL)),
    await callbackGET(new Request(profile.callbackURI)),
  ]) {
    assert.equal(response.status, 503);
    assert.equal((await response.json()).code, 'IDENTITY_NOT_CONFIGURED');
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.equal(response.headers.get('set-cookie'), null);
  }
  assert.equal(exchanges + jwksRequests, 0);
  const adapter = configureAccountEntry(startup);
  const callback = await signin();
  const success = await callbackGET(callback);
  assert.equal(success.status, 303);
  const sessionCookie = success.headers.getSetCookie().find((v) => v.startsWith('opentour-session=')).split(';')[0];
  const handle = sessionCookie.split('=')[1];
  assert.equal((await callbackGET(callback)).status, 401);
  assert.equal(exchanges, 1);
  const request = new Request(profile.origin + '/auth/operation', { method: 'POST', headers: {
    cookie: sessionCookie, origin: profile.origin, 'x-csrf-token': (await sessions.current(handle)).csrfToken,
  } });
  const identity = await adapter.verifiedIdentity(request, 'session_context');
  assert.equal(identity.session_id, config.sid);
  assert.equal('organization_id' in identity, false);

  const before = await sessions.current(handle);
  const wrongAudience = await issueToken(config.sid, { aud: 'https://foreign.example.invalid' });
  await assert.rejects(sessions.replaceCredentials(handle, identity.session_generation, {
    ...before.credentials, accessToken: wrongAudience,
  }), { code: 'SESSION_INVALID', status: 401 });
  assert.equal((await sessions.current(handle)).credentialDigest, before.credentialDigest);
  const accessToken = await issueToken(config.sid, { jti: 'generated-replacement' });
  const updatedCredentials = { ...before.credentials, accessToken,
    refreshToken: 'GENERATED_ROTATED_REFRESH_SENTINEL' };
  const replacementDigest = await sessions.replaceCredentials(
    handle, identity.session_generation, updatedCredentials,
  );
  assert.match(replacementDigest, /^[a-f0-9]{64}$/);
  assert.notEqual(replacementDigest, before.credentialDigest);
  const current = await sessions.current(handle);
  assert.equal(current.credentialDigest, replacementDigest);
  assert.equal(current.identity.session_generation, identity.session_generation);
  assert.deepEqual(current.credentials, updatedCredentials);
  assert.equal(await port('selection_digest', { handle, accessToken }), replacementDigest);
  await assert.rejects(port('selection_digest', { handle, accessToken: before.credentials.accessToken }),
    { code: 'CONTEXT_RECONCILIATION_REQUIRED', status: 409 });

  assert.equal((await adapter.signout(request)).status, 303);
  assert.equal((await adapter.signout(request)).status, 303);
  assert.deepEqual(await sessions.current(handle), { handle, csrfToken: before.csrfToken, ended: true,
    generation: identity.session_generation + 1 });
  await assert.rejects(adapter.verifiedIdentity(request, 'session_context'), IdentityFailure);
  nextSid = config.sid + '_end_unknown';
  const endFlow = await signin();
  const endAdmission = await callbackGET(endFlow);
  assert.equal(endAdmission.status, 303);
  const endCookie = endAdmission.headers.getSetCookie().find((v) => v.startsWith('opentour-session=')).split(';')[0];
  const endHandle = endCookie.split('=')[1];
  const endRequest = new Request(profile.origin + '/auth/operation', { method: 'POST', headers: {
    cookie: endCookie, origin: profile.origin, 'x-csrf-token': (await sessions.current(endHandle)).csrfToken,
  } });
  endUnknown = true;
  const endUnconfirmed = await adapter.signout(endRequest);
  assert.equal(endUnconfirmed.status, 503);
  assert.equal(endUnconfirmed.headers.get('set-cookie'), null);
  assert.equal(endUnconfirmed.headers.get('location'), null);
  assert.equal((await sessions.current(endHandle)).ended, true);
  endUnknown = false;
  assert.equal((await adapter.signout(endRequest)).status, 303);
  nextSid = config.sid + '_unknown';
  admissionUnknown = true;
  const uncertainFlow = await signin();
  const uncertain = await callbackGET(uncertainFlow);
  assert.equal(uncertain.status, 503);
  assert.equal(uncertain.headers.getSetCookie().some((v) => v.startsWith('opentour-session=')), false);
  assert.equal((await callbackGET(uncertainFlow)).status, 401);
  assert.equal(exchanges, 3);
  assert.equal(jwksRequests, 1);
  assert.equal((await port('inspect')).sealed, true);
}

const originalFetch = globalThis.fetch;
globalThis.fetch = generatedFetch;
try {
  await runProbe();
} finally {
  globalThis.fetch = originalFetch;
  await closeBridge();
}
console.log(JSON.stringify({ configured_factories: 'PASS', actual_postgresql: true,
  exported_signin_callback: true, unconfigured_exports_refused: true,
  generated_provider: true, provider_acceptance: 'NOT_RUN', exchanges,
  callback_replay_denied: true, durable_end_denied: true, unknown_commit_no_cookie: true,
  unknown_end_no_cookie_or_redirect: true,
  sealed_rows: true, mode: config.mode }));
