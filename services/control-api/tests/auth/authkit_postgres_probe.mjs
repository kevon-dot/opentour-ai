// Test-only configured factories, real local PostgreSQL ports and generated SDK responses.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
const [adapterPath, python, bridgePath, configPath] = process.argv.slice(2);
const require = createRequire(pathToFileURL(adapterPath));
const { generateKeyPair, SignJWT } = await import(pathToFileURL(require.resolve('jose')).href);
const { createAuthkitAdapter, createWorkOSProvider, IdentityFailure } = await import(pathToFileURL(adapterPath).href);
const config = JSON.parse(readFileSync(configPath, 'utf8'));
const profile = { origin: 'https://app.example.invalid', callbackURI: 'https://app.example.invalid/auth/callback',
  signedOutPath: '/signed-out', returnPaths: ['/', '/signed-out'], providerOrigins: ['https://api.workos.com'],
  issuer: config.issuer, audience: 'https://api.example.invalid', jwksURL: 'https://api.workos.com/sso/jwks/client_fixture',
  clientId: 'client_fixture' };
function port(action, data = {}) {
  const result = JSON.parse(execFileSync(python, [bridgePath, configPath], {
    input: JSON.stringify({ action, ...data }), encoding: 'utf8', timeout: 10_000, maxBuffer: 131_072,
  }));
  if (result.error) throw new IdentityFailure(result.error.code, result.error.status);
  return result.value;
}
let exchanges = 0;
let admissionUnknown = false;
let nextSid = config.sid;
const { publicKey, privateKey } = await generateKeyPair('RS256');
const sdk = createWorkOSProvider(profile, 'generated-api-key', { now: () => config.now,
  key: async () => publicKey,
  fetch: async () => {
    exchanges++;
    const token = await new SignJWT({ iss: profile.issuer, aud: profile.audience, sub: 'user_a', sid: nextSid,
      iat: config.now/1000-60, exp: config.now/1000+600, auth_time: config.now/1000-120 })
      .setProtectedHeader({ alg: 'RS256', kid: 'generated' }).sign(privateKey);
    return new Response(JSON.stringify({ access_token: token, refresh_token: 'GENERATED_REFRESH_SENTINEL',
      user: { id: 'user_a', email: 'generated@example.invalid', email_verified: true,
        first_name: null, last_name: null, profile_picture_url: null,
        created_at: new Date(config.now).toISOString(), updated_at: new Date(config.now).toISOString() } }),
    { headers: { 'content-type': 'application/json' } });
  } });
const adapter = createAuthkitAdapter({ profile, provider: sdk, now: () => config.now,
  transactions: {
    create: async (flow) => port('create_flow', { flow }),
    consume: async (state, browser, now) => port('consume_flow', { state, browser, now }),
  }, sessions: {
    admit: async (credentials, claims) => port(admissionUnknown ? 'admit_unknown' : 'admit', { credentials, claims }),
    current: async (handle) => port('current', { handle }),
    replaceCredentials: async (handle, generation, credentials, claims) => port('replace', { handle, generation, credentials, claims }),
    end: async (handle, generation) => port('end', { handle, generation }),
    requireReconciliation: async (handle, generation) => port('reconcile', { handle, generation }),
  }, selections: {
    authorize: async () => { throw new Error('Selection belongs to172 and is not invoked by signin'); },
    commit: async () => { throw new Error('Selection belongs to172 and is not invoked by signin'); },
  } });
async function signin() {
  const response = await adapter.signin(new Request(profile.origin + '/auth/signin'));
  assert.equal(response.status, 303);
  const state = new URL(response.headers.get('location')).searchParams.get('state');
  const cookie = response.headers.get('set-cookie').split(';')[0];
  return new Request(`${profile.callbackURI}?state=${state}&code=generated`, { headers: { cookie } });
}
const callback = await signin();
const success = await adapter.callback(callback);
assert.equal(success.status, 303);
const sessionCookie = success.headers.getSetCookie().find((v) => v.startsWith('opentour-session=')).split(';')[0];
const handle = sessionCookie.split('=')[1];
assert.equal((await adapter.callback(callback)).status, 401);
assert.equal(exchanges, 1);
const request = new Request(profile.origin + '/auth/operation', { method: 'POST', headers: {
  cookie: sessionCookie, origin: profile.origin, 'x-csrf-token': port('current', { handle }).csrfToken,
} });
const identity = await adapter.verifiedIdentity(request, 'session_context');
assert.equal(identity.session_id, config.sid);
assert.equal('organization_id' in identity, false);
assert.equal((await adapter.signout(request)).status, 303);
assert.equal((await adapter.signout(request)).status, 303);
await assert.rejects(adapter.verifiedIdentity(request, 'session_context'), IdentityFailure);
nextSid = config.sid + '_unknown';
admissionUnknown = true;
const uncertainFlow = await signin();
const uncertain = await adapter.callback(uncertainFlow);
assert.equal(uncertain.status, 503);
assert.equal(uncertain.headers.getSetCookie().some((v) => v.startsWith('opentour-session=')), false);
assert.equal((await adapter.callback(uncertainFlow)).status, 401);
assert.equal(exchanges, 2);
assert.equal(port('inspect').sealed, true);
console.log(JSON.stringify({ configured_factories: 'PASS', actual_postgresql: true,
  generated_provider: true, provider_acceptance: 'NOT_RUN', exchanges,
  callback_replay_denied: true, durable_end_denied: true, unknown_commit_no_cookie: true,
  sealed_rows: true, mode: config.mode }));
