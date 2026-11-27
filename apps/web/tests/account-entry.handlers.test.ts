/** Exercises the actual route exports with generated WorkOS HTTP and in-memory test ports. */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { it, mock } from 'node:test';
import { exportJWK, generateKeyPair, SignJWT } from 'jose';
import type { AccountEntryStartupOptions } from '../features/account-entry/server.js';
import type { AuthTransaction, ServerSession } from '../lib/server/authkit-adapter.js';

mock.module('server-only', { namedExports: {} });
const { configureAccountEntry } = await import('../features/account-entry/server.js');
const { IdentityFailure } = await import('../lib/server/authkit-adapter.js');
const { GET: signin } = await import('../app/auth/signin/route.js');
const { GET: callback } = await import('../app/auth/callback/route.js');
const { POST: signout } = await import('../app/auth/signout/route.js');

const now = Date.UTC(2026, 10, 18);
const issuedAt = now / 1000 - 30;
const subject = 'generated_user';
const sessionId = 'generated_session';
const handle = 'a'.repeat(43);
const csrfToken = 'generated_csrf';
const workosApiKey = 'entry-fixture-key';
const profile: AccountEntryStartupOptions['profile'] = {
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

it('shares one trusted startup across sign-in and callback with the existing protected session flow', async (t) => {
  const flows = new Map<string, AuthTransaction>();
  let admitted = 0;
  let codeExchanges = 0;
  let verifier: string | undefined;
  let committed: ServerSession | undefined;
  let endCalls = 0;
  let endConfirmed = false;
  const unexpected = async (): Promise<never> => { throw new Error('Unexpected account entry port'); };

  const { publicKey, privateKey } = await generateKeyPair('RS256');
  const key = { ...await exportJWK(publicKey), kid: 'generated_signing_key', alg: 'RS256', use: 'sig' };
  const accessToken = await new SignJWT({
    iss: profile.issuer, aud: profile.audience, sub: subject, sid: sessionId,
    iat: issuedAt, auth_time: issuedAt, exp: issuedAt + 600,
  }).setProtectedHeader({ alg: 'RS256', kid: key.kid }).sign(privateKey);
  const refreshToken = 'generated_refresh_token';

  const providerFetch = t.mock.method(globalThis, 'fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    if (request.url === profile.jwksURL) {
      assert.equal(request.method, 'GET');
      return Response.json({ keys: [key] });
    }

    assert.equal(request.url, 'https://api.workos.com/user_management/authenticate');
    assert.equal(request.method, 'POST');
    const body = await request.json();
    assert.equal(body.client_id, profile.clientId);
    assert.equal(body.client_secret, workosApiKey);
    assert.equal(body.code, 'generated_code');
    assert.equal(body.code_verifier, verifier);
    codeExchanges++;
    return Response.json({
      user: { object: 'user', id: subject },
      access_token: accessToken,
      refresh_token: refreshToken,
    });
  });

  const options: AccountEntryStartupOptions = {
    profile,
    workosApiKey,
    now: () => now,
    transactions: {
      async create(flow) {
        flows.set(`${flow.state}.${flow.nonce}`, flow);
        verifier = flow.verifier;
      },
      async consume(state, browserDigest, checkedAt) {
        const flow = flows.get(state);
        if (!flow || flow.browserDigest !== browserDigest || flow.expiresAt <= checkedAt) {
          throw new IdentityFailure('CALLBACK_INVALID');
        }
        flows.delete(state);
        return flow;
      },
    },
    sessions: {
      async admit(credentials, claims) {
        assert.equal(credentials.accessToken, accessToken);
        assert.equal(credentials.refreshToken, refreshToken);
        assert.equal(credentials.userId, subject);
        assert.equal(claims.organizationId, null);
        admitted++;
        committed = {
          handle,
          credentials,
          csrfToken,
          ended: false,
          reconciliationRequired: false,
          identity: {
            actor: { kind: 'human', subject: claims.subject, issuer: claims.issuer },
            session_id: claims.sessionId,
            session_generation: 0,
            identity_checked_at: new Date(now).toISOString(),
            authentication_time: claims.authenticationTime,
            assurance_level: 'unknown',
          },
        };
        return committed;
      },
      async current(requested) {
        assert.equal(requested, handle);
        if (!committed) throw new IdentityFailure('SESSION_INVALID');
        return committed;
      },
      replaceCredentials: unexpected,
      async end(requested, generation) {
        assert.equal(requested, handle);
        assert.equal(generation, 0);
        endCalls++;
        // An unacknowledged durable denial stays unconfirmed; the route may not claim success.
        if (!endConfirmed) throw new IdentityFailure('SESSION_END_UNCONFIRMED', 503, false);
        committed = { handle, csrfToken, ended: true, generation: generation + 1 };
        return { sessionId, generation: generation + 1 };
      },
      requireReconciliation: unexpected,
    },
    selections: { authorize: unexpected, commit: unexpected },
  };

  const signinRequest = new Request(`${profile.origin}/auth/signin`);
  const initialCallback = new Request(profile.callbackURI);
  const signoutRequest = (headers: Record<string, string>) =>
    new Request(`${profile.origin}/auth/signout`, {
      method: 'POST',
      headers: { Cookie: `opentour-session=${handle}`, ...headers },
    });
  const browserSignout = () => signoutRequest({ Origin: profile.origin, 'X-CSRF-Token': csrfToken });
  for (const result of [await signin(signinRequest), await callback(initialCallback),
    await signout(browserSignout())]) {
    assert.equal(result.status, 503);
    assert.equal((await result.json()).code, 'IDENTITY_NOT_CONFIGURED');
    assert.equal(result.headers.get('set-cookie'), null);
  }
  assert.throws(() => configureAccountEntry({ ...options, workosApiKey: ' ' }), {
    code: 'IDENTITY_CONFIGURATION_INVALID',
    status: 503,
  });
  assert.equal((await signin(signinRequest)).status, 503);
  assert.equal(providerFetch.mock.callCount(), 0);

  configureAccountEntry(options);
  assert.throws(() => configureAccountEntry(options), { code: 'IDENTITY_ALREADY_CONFIGURED', status: 503 });
  const flow = await signin(signinRequest);
  assert.equal(flow.status, 303);
  const location = flow.headers.get('location');
  assert.ok(location);
  const destination = new URL(location);
  const state = destination.searchParams.get('state');
  assert.ok(state);
  const cookie = flow.headers.get('set-cookie');
  assert.ok(cookie);
  assert.ok(cookie.includes('Secure; HttpOnly; SameSite=Lax'));
  assert.equal(destination.origin, profile.providerOrigins[0]);
  assert.equal(destination.searchParams.get('redirect_uri'), profile.callbackURI);
  assert.equal(destination.searchParams.get('code_challenge_method'), 'S256');
  assert.ok(verifier);
  assert.equal(destination.searchParams.get('code_challenge'), createHash('sha256').update(verifier).digest('base64url'));
  assert.equal(flows.size, 1);

  const callbackUrl = new URL(profile.callbackURI);
  callbackUrl.searchParams.set('state', state);
  callbackUrl.searchParams.set('code', 'generated_code');
  const callbackRequest = new Request(callbackUrl, { headers: { Cookie: cookie.split(';')[0]! } });
  const result = await callback(callbackRequest);
  assert.equal(result.status, 303);
  assert.equal(result.headers.get('location'), `${profile.origin}/`);
  assert.equal(result.headers.get('cache-control'), 'no-store');
  assert.deepEqual(result.headers.getSetCookie(), [
    'opentour-auth-flow=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0',
    `opentour-session=${handle}; Path=/; Secure; HttpOnly; SameSite=Lax`,
  ]);
  assert.equal(admitted, 1);
  assert.equal(flows.size, 0);
  assert.equal(codeExchanges, 1);
  assert.equal(providerFetch.mock.callCount(), 2);

  const replay = await callback(callbackRequest);
  assert.equal(replay.status, 401);
  assert.equal((await replay.json()).code, 'CALLBACK_INVALID');
  assert.equal(codeExchanges, 1);
  assert.equal(admitted, 1);

  // The signout route is a composition seam: the shared adapter refuses before any local denial.
  for (const refused of [
    await signout(signoutRequest({ Origin: 'https://attacker.example.invalid', 'X-CSRF-Token': csrfToken })),
    await signout(signoutRequest({ Origin: profile.origin })),
  ]) {
    assert.equal(refused.status, 403);
    assert.equal((await refused.json()).code, 'CSRF_INVALID');
    assert.equal(refused.headers.get('set-cookie'), null);
    assert.equal(refused.headers.get('location'), null);
  }
  assert.equal(endCalls, 0);

  const unconfirmed = await signout(browserSignout());
  const unconfirmedBody = await unconfirmed.json();
  assert.equal(unconfirmed.status, 503);
  assert.equal(unconfirmedBody.code, 'SESSION_END_UNCONFIRMED');
  assert.equal(unconfirmedBody.retryable, false);
  assert.equal(unconfirmed.headers.get('set-cookie'), null);
  assert.equal(unconfirmed.headers.get('location'), null);
  assert.equal(endCalls, 1);

  endConfirmed = true;
  const ended = await signout(browserSignout());
  assert.equal(ended.status, 303);
  const logout = ended.headers.get('location');
  assert.ok(logout);
  assert.equal(new URL(logout).origin, profile.providerOrigins[0]);
  assert.ok(logout.includes(sessionId));
  assert.deepEqual(ended.headers.getSetCookie(), [
    'opentour-session=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0',
  ]);
  assert.equal(ended.headers.get('cache-control'), 'no-store');
  assert.equal(endCalls, 2);
  // The configured logout destination is built locally; signout adds no provider traffic.
  assert.equal(providerFetch.mock.callCount(), 2);
});
