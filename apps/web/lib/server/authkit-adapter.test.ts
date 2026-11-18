/** Generated providers and memory ports: simulated logic, not durable/provider acceptance. */
import assert from 'node:assert/strict';
import { describe, it, mock } from 'node:test';
import { isDeepStrictEqual } from 'node:util';
import { testCases } from '../../tests/web-unit-cases.js';
import { exportJWK, generateKeyPair, SignJWT } from 'jose';
import type { ActiveServerSession, AuthProfile, AuthTransaction, Claims, Credentials, Identity,
  SelectionDecision, SelectionIntent, SelectionTarget, ServerSession } from './authkit-adapter.js';

mock.module('server-only', { namedExports: {} });
const { createAuthkitAdapter, createWorkOSProvider, IdentityFailure, ProviderFailure } =
  await import('./authkit-adapter.js');

const NOW = Date.UTC(2026, 10, 15, 12);
const profile: AuthProfile = { origin: 'https://app.example.invalid',
  callbackURI: 'https://app.example.invalid/auth/callback', signedOutPath: '/signed-out',
  returnPaths: ['/', '/setup', '/signed-out'], providerOrigins: ['https://api.workos.com'],
  issuer: 'https://identity.example.invalid/', audience: 'https://api.example.invalid',
  jwksURL: 'https://api.workos.com/sso/jwks/client_fixture', clientId: 'client_fixture' };
const claims: Claims = { issuer: profile.issuer, subject: 'user_a', sessionId: 'session_a',
  organizationId: 'identity_org_a', authenticationTime: new Date(NOW - 60_000).toISOString(), expiresAt: NOW + 600_000 };
const credentials: Credentials = { accessToken: 'GENERATED_ACCESS_SENTINEL',
  refreshToken: 'GENERATED_REFRESH_SENTINEL', userId: 'user_a' };
const targetA: SelectionTarget = { organizationId: 'org_a', identityOrganizationId: 'identity_org_a',
  identityIssuer: profile.issuer, membershipId: 'member_a', authorityRevision: 'authority_a' };
const selectionKey = 'generated-selection-intent';
const stagedDigest = 'a'.repeat(64);
const intentFor = (organizationId: string) => ({ organizationId, expectedGeneration: 0, idempotencyKey: selectionKey });
const targetB: SelectionTarget = { organizationId: 'org_b', identityOrganizationId: 'identity_org_b',
  identityIssuer: profile.issuer, membershipId: 'member_b', authorityRevision: 'authority_b' };

function fixture() {
  let currentTime = NOW;
  const flows = new Map<string, AuthTransaction>();
  const session: Omit<ActiveServerSession, 'ended'> & { ended: boolean } = {
    handle: 'h'.repeat(43), ended: false, reconciliationRequired: false,
    csrfToken: 'csrf-fixture', credentials, credentialDigest: stagedDigest,
    identity: { actor: { kind: 'human', subject: claims.subject, issuer: claims.issuer },
      session_id: claims.sessionId, session_generation: 0,
      identity_checked_at: new Date(NOW).toISOString(), authentication_time: claims.authenticationTime,
      assurance_level: 'unknown' } };
  function readSession(): ServerSession {
    if (session.ended) {
      return { handle: session.handle, csrfToken: session.csrfToken, ended: true,
        generation: session.identity.session_generation };
    }
    return { ...structuredClone(session), ended: false };
  }
  const sessions = {
    admit: mock.fn(async () => {
      if (session.ended) throw new IdentityFailure('SESSION_ENDED');
      return readSession();
    }),
    current: mock.fn(async () => readSession()),
    replaceCredentials: mock.fn(async (_handle: string, generation: number, next: Credentials) => {
      if (session.ended || generation !== session.identity.session_generation) throw new IdentityFailure('STALE_SESSION_CONTEXT');
      session.credentials = next;
      return stagedDigest;
    }),
    end: mock.fn(async (_handle: string, _generation: number) => {
      if (!session.ended) session.identity.session_generation++;
      session.ended = true;
      return { sessionId: session.identity.session_id, generation: session.identity.session_generation };
    }),
    requireReconciliation: mock.fn(async () => { session.reconciliationRequired = true; }),
  };
  const transactions = {
    create: mock.fn(async (flow: AuthTransaction) => { flows.set(`${flow.state}.${flow.nonce}`, structuredClone(flow)); }),
    consume: mock.fn(async (state: string, browser: string, now: number) => {
      const found = flows.get(state);
      if (!found || found.browserDigest !== browser || found.expiresAt <= now) throw new IdentityFailure('CALLBACK_INVALID');
      flows.delete(state);
      return structuredClone(found);
    }),
  };
  const provider = {
    authorizationURL: mock.fn(async (state: string, _verifier?: string, _organization?: string) => `https://api.workos.com/user_management/authorize?state=${state}`),
    exchange: mock.fn(async () => credentials), refresh: mock.fn(async () => credentials),
    verify: mock.fn(async () => ({ ...claims })),
    logoutURL: mock.fn(() => 'https://api.workos.com/user_management/sessions/logout'),
  };
  const selections = {
    authorize: mock.fn(async (_handle: string, _identity: Identity, intent: SelectionIntent): Promise<SelectionDecision> => {
      if (intent.organizationId === 'org_a') return { target: { ...targetA } };
      if (intent.organizationId === 'org_b') return { target: { ...targetB } };
      throw new IdentityFailure('MEMBERSHIP_INACTIVE', 403);
    }),
    commit: mock.fn(async (_handle: string, _identity: Identity, intent: SelectionIntent, target: SelectionTarget) => {
      session.identity.session_generation = intent.expectedGeneration + 1;
      session.reconciliationRequired = false;
      return { state: 'active' as const, organization_id: target.organizationId,
        membership_id: target.membershipId, session_generation: session.identity.session_generation, request_id: 'generated-request' };
    }),
  };
  const adapter = createAuthkitAdapter({ profile, provider, transactions, sessions, selections, now: () => currentTime });
  const request = (csrf = true) => new Request(`${profile.origin}/auth/operation`, {
    method: 'POST', headers: { Cookie: `opentour-session=${session.handle}`, Origin: profile.origin,
      'Idempotency-Key': selectionKey,
      ...(csrf ? { 'X-CSRF-Token': session.csrfToken } : {}) } });
  async function signin() {
    const response = await adapter.signin(new Request(`${profile.origin}/auth/signin?return_to=/setup`));
    const state = new URL(response.headers.get('location')!).searchParams.get('state')!;
    const cookie = response.headers.get('set-cookie')!.split(';')[0]!;
    const callback = new Request(`${profile.callbackURI}?state=${state}&code=generated-code`, { headers: { Cookie: cookie } });
    return { response, callback, state, cookie };
  }
  return { adapter, provider, sessions, session, transactions, selections, flows, request, signin,
    advance: (ms: number) => { currentTime += ms; } };
}

describe('server adapter admission', () => {
  it('allows public signin and commits before emitting a usable session cookie', async () => {
    const f = fixture(); const flow = await f.signin();
    assert.equal(flow.response.status, 303);
    assert.ok((flow.response.headers.get('set-cookie'))?.includes('Secure; HttpOnly; SameSite=Lax'));
    assert.equal(f.sessions.current.mock.callCount(), 0);
    const result = await f.adapter.callback(flow.callback);
    assert.equal(result.status, 303);
    assert.equal(result.headers.get('location'), `${profile.origin}/setup`);
    assert.equal(f.sessions.admit.mock.callCount(), 1);
    assert.ok((result.headers.get('set-cookie'))?.includes('opentour-session='));
    assert.equal(result.headers.get('cache-control'), 'no-store');
    assert.ok((JSON.stringify([...result.headers]))?.includes('SENTINEL') === false);
  });
  testCases(['https://foreign.invalid', '//foreign.invalid', '/\\foreign', '/%2fforeign', '/not-allowed'])('rejects unsafe return %s before creating a flow', async (path) => {
      const f = fixture(); const response = await f.adapter.signin(new Request(
        `${profile.origin}/auth/signin?return_to=${encodeURIComponent(path)}`));
      assert.equal(response.status, 422); assert.equal(f.transactions.create.mock.callCount(), 0);
    });
  it('ignores forwarded redirect configuration', async () => {
    const f = fixture(); const result = await f.adapter.signin(new Request(`${profile.origin}/auth/signin`,
      { headers: { 'x-redirect-uri': 'https://foreign.invalid', 'x-forwarded-host': 'foreign.invalid' } }));
    assert.equal(result.status, 303); assert.ok((result.headers.get('location'))?.includes('foreign') === false);
  });
  it('consumes once across two concurrent callback attempts', async () => {
    const f = fixture(); const flow = await f.signin();
    const results = await Promise.all([f.adapter.callback(flow.callback), f.adapter.callback(flow.callback)]);
    assert.deepEqual(results.map((r) => r.status).sort(), [303, 401]);
    assert.equal(f.provider.exchange.mock.callCount(), 1);
  });
  testCases(['foreign-browser', 'expired', 'duplicate-state', 'duplicate-code', 'nonce'])('refuses callback %s before code exchange', async (fault) => {
      const f = fixture(); const flow = await f.signin(); let req = flow.callback;
      if (fault === 'expired') f.advance(600_000);
      if (fault === 'foreign-browser') req = new Request(req.url, { headers: { Cookie: `opentour-auth-flow=${'z'.repeat(43)}` } });
      if (fault === 'duplicate-state') req = new Request(req.url + '&state=duplicate', { headers: req.headers });
      if (fault === 'duplicate-code') req = new Request(req.url + '&code=duplicate', { headers: req.headers });
      if (fault === 'nonce') req = new Request(req.url.replace(flow.state, flow.state.slice(0, 44) + 'z'.repeat(43)), { headers: req.headers });
      assert.equal((await f.adapter.callback(req)).status, 401);
      assert.equal(f.provider.exchange.mock.callCount(), 0); assert.equal(f.sessions.admit.mock.callCount(), 0);
    });
  testCases(['claim', 'admission', 'provider'])('does not mint success for unknown %s outcome', async (fault) => {
    const f = fixture(); const flow = await f.signin();
    const error = new Error('GENERATED_REFRESH_SENTINEL');
    if (fault === 'claim') f.transactions.consume.mock.mockImplementation(async () => { throw error; });
    if (fault === 'admission') f.sessions.admit.mock.mockImplementation(async () => { throw error; });
    if (fault === 'provider') f.provider.exchange.mock.mockImplementation(async () => { throw error; });
    const log = mock.method(console, 'error', () => undefined);
    try {
      const response = await f.adapter.callback(flow.callback);
      assert.equal(response.status, 503); assert.ok((response.headers.get('set-cookie'))?.includes('opentour-session=') === false);
      assert.ok((await response.text())?.includes('SENTINEL') === false); assert.equal(log.mock.callCount(), 0);
    } finally { log.mock.restore(); }
  });
  it('checks callback expiry again after provider exchange', async () => {
    const f = fixture(); const flow = await f.signin();
    f.provider.exchange.mock.mockImplementation(async () => { f.advance(600_000); return credentials; });
    assert.equal((await f.adapter.callback(flow.callback)).status, 401);
    assert.equal(f.sessions.admit.mock.callCount(), 0);
  });
  it('callback never invokes invitation or selection for ordinary signin', async () => {
    const f = fixture(); const flow = await f.signin(); await f.adapter.callback(flow.callback);
    assert.equal(f.selections.commit.mock.callCount(), 0); assert.equal(f.selections.authorize.mock.callCount(), 0);
  });
  testCases(['ended', 'generation'])('rechecks %s after deferred token verification', async (change) => {
    const f = fixture(); f.provider.verify.mock.mockImplementation(async () => {
      if (change === 'ended') f.session.ended = true; else f.session.identity.session_generation++;
      return claims;
    });
    await assert.rejects(f.adapter.verifiedIdentity(f.request(), 'session_context'), IdentityFailure);
  });
  it('unscoped identity exposes no tenant authority during reconciliation', async () => {
    const f = fixture(); f.session.reconciliationRequired = true;
    const identity = await f.adapter.verifiedIdentity(f.request(), 'session_context');
    assert.equal(identity.assurance_level, 'unknown'); assert.equal('organization_id' in identity, false);
    await assert.rejects(f.adapter.verifiedIdentity(f.request(), 'tenant_read' as never), IdentityFailure);
  });
  it('projects only verified identity fields from an enriched session port', async () => {
    const f = fixture();
    Object.assign(f.session.identity, {
      organization_id: targetA.organizationId,
      membership_id: targetA.membershipId,
      delegated_grant_id: 'generated_grant',
      capabilities: ['generated:capability'],
      session_generation: 4,
      authentication_time: new Date(NOW - 120_000).toISOString(),
    });
    Object.assign(f.session.identity.actor, { profile_label: 'generated private label' });
    f.advance(5_000);

    assert.deepEqual(await f.adapter.verifiedIdentity(f.request(), 'session_context'), {
      actor: { kind: 'human', subject: claims.subject, issuer: claims.issuer },
      session_id: claims.sessionId,
      session_generation: 4,
      identity_checked_at: new Date(NOW + 5_000).toISOString(),
      authentication_time: claims.authenticationTime,
      assurance_level: 'unknown',
    });
  });
});

describe('ending and switching', () => {
  it('confirms only the exact verified target after current membership and local commit', async () => {
    const f = fixture();
    const identity = structuredClone(f.session.identity);
    const outcome = await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup');
    assert.deepEqual(outcome, { outcome: 'verified_target', receipt: {
      state: 'active', organization_id: 'org_a', membership_id: 'member_a', session_generation: 1, request_id: 'generated-request',
    } });
    assert.equal(f.selections.authorize.mock.callCount(), 2);
    assert.deepEqual(f.provider.refresh.mock.calls.map(call => call.arguments), [[credentials, 'identity_org_a']]);
    assert.deepEqual(f.selections.commit.mock.calls.map(call => call.arguments), [
      [f.session.handle, identity, intentFor('org_a'), targetA, claims, stagedDigest],
    ]);
    assert.equal(f.session.reconciliationRequired, false);
  });
  it('returns a historical receipt for the exact retained intent without refreshing or restoring the old selection', async () => {
    const f = fixture();
    f.session.identity.session_generation = 3;
    const receipt = { state: 'active' as const, organization_id: 'org_a',
      membership_id: 'member_a', session_generation: 1, request_id: 'original-request' };
    f.selections.authorize.mock.mockImplementation(async () => ({ receipt }));
    assert.deepEqual(await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup'),
      { outcome: 'replayed', receipt });
    assert.equal(f.sessions.requireReconciliation.mock.callCount(), 0);
    assert.equal(f.provider.refresh.mock.callCount(), 0);
    assert.equal(f.selections.commit.mock.callCount(), 0);
    assert.equal(f.session.identity.session_generation, 3);
  });
  testCases(['ended', 'expired'] as const)('refuses a historical receipt after the bound identity becomes %s', async (state) => {
    const f = fixture();
    f.selections.authorize.mock.mockImplementation(async () => {
      if (state === 'ended') f.session.ended = true;
      else f.advance(600_000);
      return { receipt: { state: 'active', organization_id: 'org_a',
        membership_id: 'member_a', session_generation: 1, request_id: 'original-request' } };
    });
    await assert.rejects(f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup'),
      { code: state === 'ended' ? 'SESSION_ENDED' : 'SESSION_INVALID' });
    assert.equal(f.provider.refresh.mock.callCount(), 0);
  });
  it('rechecks the bound session after token verification and before contacting selection authority', async () => {
    const f = fixture();
    f.provider.verify.mock.mockImplementation(async () => { f.session.ended = true; return claims; });
    await assert.rejects(f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup'), { code: 'SESSION_ENDED' });
    assert.equal(f.selections.authorize.mock.callCount(), 0);
  });
  it('records a verified current organization without another refresh or credential write', async () => {
    const f = fixture();
    f.selections.authorize.mock.mockImplementation(async () => ({ target: targetA, alreadyCurrent: true }));
    f.selections.commit.mock.mockImplementation(async () => ({ state: 'active', organization_id: 'org_a',
      membership_id: 'member_a', session_generation: 0, request_id: 'no-op' }));
    const result = await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup');
    assert.equal(result.outcome, 'verified_target');
    assert.equal(f.provider.refresh.mock.callCount(), 0);
    assert.equal(f.sessions.requireReconciliation.mock.callCount(), 0);
    assert.equal(f.sessions.replaceCredentials.mock.callCount(), 0);
  });
  it('refuses a current-organization receipt when another selection becomes pending', async () => {
    const f = fixture();
    f.selections.authorize.mock.mockImplementation(async () => {
      f.session.reconciliationRequired = true;
      return { target: targetA, alreadyCurrent: true };
    });
    await assert.rejects(f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup'),
      { code: 'CONTEXT_RECONCILIATION_REQUIRED' });
    assert.equal(f.provider.refresh.mock.callCount(), 0);
    assert.equal(f.selections.commit.mock.callCount(), 0);
  });
  it('refuses selection without a stable intent key', async () => {
    const f = fixture();
    const request = f.request();
    request.headers.delete('Idempotency-Key');
    await assert.rejects(f.adapter.switchOrganization(request, 'org_a', 0, '/setup'), { code: 'SCHEMA_INVALID' });
    assert.equal(f.selections.authorize.mock.callCount(), 0);
    assert.equal(f.provider.refresh.mock.callCount(), 0);
  });
  it('refuses selection after an unconfirmed credential write', async () => {
    const f = fixture();
    f.sessions.replaceCredentials.mock.mockImplementation(async () => '');
    assert.equal((await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup')).outcome, 'unknown');
    assert.equal(f.selections.commit.mock.callCount(), 0);
    assert.equal(f.session.reconciliationRequired, true);
  });
  it('refreshes with the latest sealed credentials when the access token is unchanged', async () => {
    const f = fixture();
    const rotated = { ...credentials, refreshToken: 'GENERATED_ROTATED_REFRESH' };
    f.selections.authorize.mock.mockImplementationOnce(async () => {
      f.session.credentials = rotated;
      return { target: targetA };
    }, 0);
    assert.equal((await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup')).outcome, 'verified_target');
    assert.deepEqual(f.provider.refresh.mock.calls.map(({ arguments: args }) => args),
      [[rotated, 'identity_org_a']]);
  });
  it('requires a durable pending marker before any provider context mutation', async () => {
    const f = fixture(); f.sessions.requireReconciliation.mock.mockImplementation(async () => { throw new Error('unknown local state'); });
    await assert.rejects(f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup'));
    assert.equal(f.provider.refresh.mock.callCount(), 0); assert.equal(f.selections.commit.mock.callCount(), 0);
  });
  it('checks CSRF before mutation', async () => {
    const f = fixture(); assert.equal((await f.adapter.signout(f.request(false))).status, 403);
    assert.equal(f.sessions.end.mock.callCount(), 0);
  });
  it('checks CSRF before requesting an ended session receipt', async () => {
    const f = fixture();
    f.session.ended = true;
    f.session.identity.session_generation = 1;
    const request = f.request();
    request.headers.set('X-CSRF-Token', 'csrf-another');

    const result = await f.adapter.signout(request);
    assert.equal(result.status, 403);
    assert.equal((await result.json()).code, 'CSRF_INVALID');
    assert.equal(result.headers.get('set-cookie'), null);
    assert.equal(result.headers.get('location'), null);
    assert.equal(f.sessions.end.mock.callCount(), 0);
    assert.equal(f.provider.logoutURL.mock.callCount(), 0);
  });
  it('clears the same cookie on signout replay with a minimal ended record', async () => {
    const f = fixture();
    const first = await f.adapter.signout(f.request());
    const clearingCookie = first.headers.get('set-cookie');
    assert.equal(first.status, 303);
    assert.equal(first.headers.get('location'), 'https://api.workos.com/user_management/sessions/logout');
    assert.match(clearingCookie ?? '', /^opentour-session=; .*Max-Age=0/);
    assert.deepEqual(await f.sessions.current(), {
      handle: f.session.handle, csrfToken: f.session.csrfToken, ended: true, generation: 1,
    });

    const repeat = await f.adapter.signout(f.request());
    assert.equal(repeat.status, 303);
    assert.equal(repeat.headers.get('location'), first.headers.get('location'));
    assert.equal(repeat.headers.get('set-cookie'), clearingCookie);
    assert.equal(repeat.headers.get('cache-control'), 'no-store');
    assert.deepEqual(f.sessions.end.mock.calls.map(call => call.arguments), [
      [f.session.handle, 0], [f.session.handle, 1],
    ]);
    assert.equal(f.session.identity.session_generation, 1);
    assert.equal(f.provider.logoutURL.mock.callCount(), 2);
  });
  testCases(['active', 'ended'] as const)('refuses signout with a mismatched %s session handle', async (state) => {
    const f = fixture();
    const request = f.request();
    f.session.handle = 'x'.repeat(43);
    f.session.ended = state === 'ended';

    const result = await f.adapter.signout(request);
    assert.equal(result.status, 401);
    assert.equal((await result.json()).code, 'SESSION_INVALID');
    assert.equal(result.headers.get('location'), null);
    assert.equal(result.headers.get('set-cookie'), null);
    assert.equal(f.sessions.end.mock.callCount(), 0);
    assert.equal(f.provider.logoutURL.mock.callCount(), 0);
  });
  it('provider logout failure cannot undo committed local denial', async () => {
    const f = fixture(); f.provider.logoutURL.mock.mockImplementation(() => { throw new Error('SECRET'); });
    const result = await f.adapter.signout(f.request());
    assert.equal(result.status, 200); assert.equal(f.session.ended, true);
    assert.deepEqual(await result.json(), { local_ended: true, provider_completion: 'unconfirmed' });
  });
  it('unknown local denial emits no success cookie or redirect', async () => {
    const f = fixture(); f.sessions.end.mock.mockImplementation(async () => { throw new Error('SECRET'); });
    const result = await f.adapter.signout(f.request());
    assert.equal(result.status, 503); assert.equal(result.headers.get('location'), null);
    assert.equal(result.headers.get('set-cookie'), null); assert.equal(f.provider.logoutURL.mock.callCount(), 0);
  });
  testCases([
    ['empty session ID', true, { sessionId: '', generation: 2 }],
    ['blank session ID', true, { sessionId: '  ', generation: 2 }],
    ['missing session ID', true, { generation: 2 }],
    ['wrong active session ID', false, { sessionId: 'foreign_session', generation: 3 }],
    ['non-advancing active generation', false, { sessionId: claims.sessionId, generation: 2 }],
    ['mismatched ended replay generation', true, { sessionId: claims.sessionId, generation: 3 }],
  ] as const)('refuses signout receipt with %s', async (_label, alreadyEnded, receipt) => {
    const f = fixture();
    f.session.ended = alreadyEnded;
    f.session.identity.session_generation = 2;
    f.sessions.end.mock.mockImplementation(async () =>
      receipt as Awaited<ReturnType<typeof f.sessions.end>>);

    const result = await f.adapter.signout(f.request());
    assert.equal(result.status, 503);
    assert.equal((await result.json()).code, 'SESSION_END_UNCONFIRMED');
    assert.equal(result.headers.get('set-cookie'), null);
    assert.equal(result.headers.get('location'), null);
    assert.deepEqual(f.sessions.end.mock.calls.map(call => call.arguments), [[f.session.handle, 2]]);
    assert.equal(f.provider.logoutURL.mock.callCount(), 0);
  });
  testCases(['subject', 'sessionId', 'organizationId'])('rejects changed refresh %s', async (field) => {
    const f = fixture();
    f.provider.verify.mock.mockImplementation(async () => ({ ...claims, [field]: 'foreign' }));
    f.provider.verify.mock.mockImplementationOnce(async () => ({ ...claims }), 0);
    assert.equal((await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup')).outcome, 'unknown');
    assert.equal(f.selections.commit.mock.callCount(), 0); assert.equal(f.session.reconciliationRequired, true);
  });
  it('logout racing refresh cannot write credentials or select', async () => {
    const f = fixture(); f.provider.refresh.mock.mockImplementation(async () => { f.session.ended = true; return credentials; });
    assert.equal((await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup')).outcome, 'unknown');
    assert.equal(f.sessions.replaceCredentials.mock.callCount(), 0); assert.equal(f.selections.commit.mock.callCount(), 0);
  });
  it('provider refresh and failed local commit remain separately unknown', async () => {
    const f = fixture(); f.selections.commit.mock.mockImplementation(async () => { throw new Error('commit ack lost'); });
    assert.equal((await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup')).outcome, 'unknown');
    assert.equal(f.session.reconciliationRequired, true); assert.equal(f.session.ended, false);
  });
  testCases(['denied', 'transient', 'unknown'] as const)('preserves provider %s without false logout', async (outcome) => {
    const f = fixture(); f.provider.refresh.mock.mockImplementation(async () => { throw new ProviderFailure(outcome); });
    assert.equal((await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup')).outcome, outcome);
    assert.equal(f.session.ended, false); assert.equal(f.selections.commit.mock.callCount(), 0);
  });
  it('creates a bound SSO continuation without trusting arbitrary provider URLs', async () => {
    const f = fixture(); f.provider.refresh.mock.mockImplementation(async () => { throw new ProviderFailure('authentication_required'); });
    const outcome = await f.adapter.switchOrganization(f.request(), 'org_b', 0, '/setup');
    assert.equal(outcome.outcome, 'authentication_required');
    const flow = [...f.flows.values()][0]!;
    assert.deepEqual([flow.selectionIntent, flow.selectionTarget, flow.sessionId, flow.subject, flow.generation],
      [intentFor('org_b'), targetB, 'session_a', 'user_a', 0]);
    assert.equal((flow.browserDigest)?.length, 64); assert.equal((flow.nonce)?.length, 43);
  });
  it('rejects an SSO callback after another selection advances the generation', async () => {
    const f = fixture(); f.provider.refresh.mock.mockImplementation(async () => { throw new ProviderFailure('authentication_required'); });
    const outcome = await f.adapter.switchOrganization(f.request(), 'org_b', 0, '/setup');
    if (outcome.outcome !== 'authentication_required') throw new Error('Missing continuation');
    const state = new URL(outcome.response.headers.get('location')!).searchParams.get('state');
    const cookie = outcome.response.headers.get('set-cookie')!.split(';')[0]!;
    f.session.identity.session_generation++;
    const response = await f.adapter.callback(new Request(`${profile.callbackURI}?state=${state}&code=generated`,
      { headers: { cookie } }));
    assert.equal(response.status, 409); assert.equal(f.provider.exchange.mock.callCount(), 0);
    assert.equal(f.selections.commit.mock.callCount(), 0);
  });
  testCases(['missing intent key', 'expired flow', 'changed generation'] as const)(
    'refuses SSO exchange after %s', async (fault) => {
      const f = fixture();
      f.provider.refresh.mock.mockImplementation(async () => { throw new ProviderFailure('authentication_required'); });
      const outcome = await f.adapter.switchOrganization(f.request(), 'org_b', 0, '/setup');
      if (outcome.outcome !== 'authentication_required') throw new Error('Missing continuation');
      if (fault === 'missing intent key') {
        Reflect.deleteProperty([...f.flows.values()][0]!.selectionIntent!, 'idempotencyKey');
      } else {
        f.selections.authorize.mock.mockImplementationOnce(async () => {
          if (fault === 'changed generation') f.session.identity.session_generation++;
          else f.advance(600_000);
          return { target: targetB };
        }, 2);
      }
      const state = new URL(outcome.response.headers.get('location')!).searchParams.get('state');
      const cookie = outcome.response.headers.get('set-cookie')!.split(';')[0]!;
      const response = await f.adapter.callback(new Request(`${profile.callbackURI}?state=${state}&code=generated`,
        { headers: { cookie } }));
      assert.equal(response.status, fault === 'changed generation' ? 409 : fault === 'expired flow' ? 401 : 422);
      assert.equal(f.provider.exchange.mock.callCount(), 0);
      assert.equal(f.selections.commit.mock.callCount(), 0);
    });
  testCases(['prior expired', 'returned expired', 'returned fresh'] as const)(
    'applies the current-token continuation boundary when %s', async (scenario) => {
      const f = fixture();
      const prior = { ...claims, expiresAt: NOW + 100 };
      f.provider.verify.mock.mockImplementationOnce(async () => prior, 0);
      f.provider.refresh.mock.mockImplementation(async () => { throw new ProviderFailure('authentication_required'); });
      const outcome = await f.adapter.switchOrganization(f.request(), 'org_b', 0, '/setup');
      if (outcome.outcome !== 'authentication_required') throw new Error('Missing continuation');

      const callbackTime = NOW + (scenario === 'prior expired' ? 100 : 99);
      f.advance(callbackTime - NOW);
      const flow = [...f.flows.values()][0]!;
      assert.ok(flow.expiresAt > callbackTime);
      if (scenario === 'prior expired') {
        // Model the configured Python authorize_current port, which requires a fresh stored token.
        assert.equal(prior.expiresAt, callbackTime);
        f.selections.authorize.mock.mockImplementationOnce(async () => {
          throw new IdentityFailure('SESSION_INVALID');
        }, 2);
      }
      const fresh = { ...claims, organizationId: targetB.identityOrganizationId,
        expiresAt: scenario === 'returned expired' ? callbackTime : callbackTime + 100_000 };
      f.provider.verify.mock.mockImplementation(async () => fresh);
      const returned = { ...credentials, accessToken: 'GENERATED_CONTINUATION_ACCESS' };
      f.provider.exchange.mock.mockImplementation(async () => returned);

      const state = new URL(outcome.response.headers.get('location')!).searchParams.get('state');
      const cookie = outcome.response.headers.get('set-cookie')!.split(';')[0]!;
      const request = new Request(`${profile.callbackURI}?state=${state}&code=generated`, { headers: { cookie } });
      const response = await f.adapter.callback(request);
      const accepted = scenario === 'returned fresh';
      assert.equal(response.status, accepted ? 303 : 401);
      if (!accepted) {
        assert.equal((await response.json()).code, scenario === 'prior expired' ? 'SESSION_INVALID' : 'CALLBACK_INVALID');
        assert.equal(response.headers.get('cache-control'), 'no-store');
        assert.equal(response.headers.get('location'), null);
        assert.deepEqual(response.headers.getSetCookie(), [
          'opentour-auth-flow=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0',
        ]);
      }
      assert.equal(f.provider.exchange.mock.callCount(), scenario === 'prior expired' ? 0 : 1);
      assert.equal(f.sessions.replaceCredentials.mock.callCount(), accepted ? 1 : 0);
      assert.equal(f.selections.commit.mock.callCount(), accepted ? 1 : 0);
      assert.equal(f.session.identity.session_generation, accepted ? 1 : 0);
      assert.equal(f.session.reconciliationRequired, !accepted);
      assert.deepEqual(f.session.credentials, accepted ? returned : credentials);
      assert.equal(f.flows.size, 0);

      const replay = await f.adapter.callback(request);
      assert.equal(replay.status, 401);
      assert.equal((await replay.json()).code, 'CALLBACK_INVALID');
      assert.equal(f.provider.exchange.mock.callCount(), scenario === 'prior expired' ? 0 : 1);

      if (scenario === 'prior expired') {
        const pending = structuredClone(f.session);
        const restart = await f.signin();
        assert.equal(restart.response.status, 303);
        assert.notEqual(restart.state, state);
        assert.notEqual(restart.cookie, cookie);
        assert.equal([...f.flows.values()][0]!.kind, 'signin');

        const recoveryProof = { ...fresh, sessionId: 'session_recovery', organizationId: null };
        const recoveryCredentials = { ...credentials, accessToken: 'GENERATED_RECOVERY_ACCESS' };
        const recoverySession = { ...pending, ended: false as const, handle: 'n'.repeat(43),
          reconciliationRequired: false, credentials: recoveryCredentials,
          identity: { ...pending.identity, session_id: recoveryProof.sessionId } };
        f.provider.exchange.mock.mockImplementationOnce(async () => recoveryCredentials);
        f.provider.verify.mock.mockImplementationOnce(async () => recoveryProof);
        f.sessions.admit.mock.mockImplementationOnce(async () => recoverySession);
        const recovered = await f.adapter.callback(restart.callback);

        assert.equal(recovered.status, 303);
        assert.equal(recovered.headers.get('location'), `${profile.origin}/setup`);
        assert.ok(recovered.headers.getSetCookie().some(value =>
          value.startsWith(`opentour-session=${recoverySession.handle};`)));
        assert.equal(f.provider.exchange.mock.callCount(), 1);
        assert.deepEqual(f.sessions.admit.mock.calls.map(call => call.arguments), [
          [recoveryCredentials, recoveryProof],
        ]);
        assert.equal(f.sessions.replaceCredentials.mock.callCount(), 0);
        assert.equal(f.selections.commit.mock.callCount(), 0);
        assert.deepEqual(f.session, pending);
        assert.equal(f.flows.size, 0);
      }
    });
  it('rechecks local membership after provider refresh and before selection', async () => {
    const f = fixture();
    f.selections.authorize.mock.mockImplementationOnce(async () => ({ target: targetA }), 0);
    f.selections.authorize.mock.mockImplementationOnce(async () => { throw new IdentityFailure('MEMBERSHIP_INACTIVE', 403); }, 1);
    assert.equal((await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup')).outcome, 'unknown');
    assert.equal(f.selections.commit.mock.callCount(), 0); assert.equal(f.session.reconciliationRequired, true);
  });
});

describe('actual maintained SDK and JWT boundary with generated inputs', () => {
  it('bounds received streaming bytes and aborts the provider body', async () => {
    let observedSignal: AbortSignal | undefined;
    const cancelled = mock.fn();
    const sdk = createWorkOSProvider(profile, 'generated-api-key', { fetch: async (_input, init) => {
      assert.equal(init?.redirect, 'error'); observedSignal = init?.signal ?? undefined;
      return new Response(new ReadableStream({ start(controller) {
        controller.enqueue(new Uint8Array(40_000)); controller.enqueue(new Uint8Array(40_000));
      }, cancel: cancelled }));
    } });
    await assert.rejects(sdk.exchange('generated-code', 'verifier'), ProviderFailure);
    assert.equal(observedSignal?.aborted, true); assert.equal(cancelled.mock.callCount(), 1);
  });
  it('applies its five-second deadline to a body that never finishes', async () => {
    mock.timers.enable({ apis: ['setTimeout'] });
    const cancelled = mock.fn();
    try {
      const sdk = createWorkOSProvider(profile, 'generated-api-key', { fetch: async () =>
        new Response(new ReadableStream({ cancel: cancelled })) });
      const operation = assert.rejects(sdk.exchange('generated-code', 'verifier'), ProviderFailure);
      await new Promise<void>(resolve => setImmediate(resolve));
      mock.timers.tick(5_001);
      await operation; assert.equal(cancelled.mock.callCount(), 1);
    } finally { mock.timers.reset(); }
  });
  it('keeps only unexpired cached keys usable during one unknown-key refresh', async () => {
    const { publicKey, privateKey } = await generateKeyPair('RS256');
    const jwk = { ...await exportJWK(publicKey), kid: 'known', alg: 'RS256' };
    const document = { keys: [jwk] };
    let monotonic = 0;
    const clock = mock.method(performance, 'now', () => monotonic);
    let release: (response: Response) => void = () => undefined;
    const transport = mock.fn(async () => new Response(JSON.stringify(document)));
    const sdk = createWorkOSProvider(profile, 'generated-api-key', { fetch: transport, now: () => NOW });
    const token = (kid: string) => new SignJWT({ iss: profile.issuer, aud: profile.audience,
      sub: 'user_a', sid: 'session_a', iat: NOW/1000-60, exp: NOW/1000+600, auth_time: NOW/1000-120 })
      .setProtectedHeader({ alg: 'RS256', kid }).sign(privateKey);
    try {
      const known = await token('known'); await sdk.verify(known);
      monotonic = 31_000;
      transport.mock.mockImplementationOnce(async () => new Promise<Response>((resolve) => { release = resolve; }));
      const pending = assert.rejects(sdk.verify(await token('unknown')), IdentityFailure);
      await Promise.resolve();
      assert.equal((await sdk.verify(known)).subject, 'user_a');
      monotonic = 300_001;
      await assert.rejects(sdk.verify(known), { status: 503 });
      release(new Response(JSON.stringify(document))); await pending;
      assert.equal(transport.mock.callCount(), 2);
    } finally { clock.mock.restore(); }
  });
  it('uses SDK PKCE challenge and sends the bound verifier only during exchange', async () => {
    const verifier = 'generated-private-pkce-verifier';
    let sentBody = '';
    const sdk = createWorkOSProvider(profile, 'generated-api-key', { fetch: async (_url, init) => {
      sentBody = String(init?.body);
      return new Response(JSON.stringify({ error: 'invalid_grant' }), { status: 400,
        headers: { 'content-type': 'application/json' } });
    } });
    const url = new URL(await sdk.authorizationURL('bound-state-and-nonce', verifier));
    assert.equal(url.searchParams.get('code_challenge_method'), 'S256');
    assert.equal((url.searchParams.get('code_challenge'))?.length, 43);
    assert.equal(url.searchParams.get('state'), 'bound-state-and-nonce');
    assert.ok((url.href)?.includes(verifier) === false);
    await assert.rejects(sdk.exchange('generated-code', verifier), { outcome: 'denied' });
    assert.equal(JSON.parse(sentBody).code_verifier, verifier);
  });
  it('does not retry or leak a credential-bearing SDK mutation fault', async () => {
    const transport = mock.fn(async () => new Response(JSON.stringify({ message: 'GENERATED_REFRESH_SENTINEL' }),
      { status: 500, headers: { 'Content-Type': 'application/json' } }));
    const sdk = createWorkOSProvider(profile, 'generated-api-key', { fetch: transport });
    let error: unknown;
    try { await sdk.exchange('generated-code', 'verifier'); } catch (caught) { error = caught; }
    assert.ok(error instanceof ProviderFailure); assert.ok((String(error))?.includes('SENTINEL') === false);
    assert.equal(transport.mock.callCount(), 1);
  });
  it('requires signed audience and current claims', async () => {
    const { privateKey, publicKey } = await generateKeyPair('RS256');
    const sdk = createWorkOSProvider(profile, 'generated-api-key', { key: async () => publicKey, now: () => NOW });
    for (const aud of [profile.audience, 'wrong', undefined]) {
      const payload = { iss: profile.issuer, ...(aud ? { aud } : {}), sub: 'user_a', sid: 'session_a',
        iat: NOW/1000-60, exp: NOW/1000+60, auth_time: NOW/1000-120 };
      const token = await new SignJWT(payload).setProtectedHeader({ alg: 'RS256', kid: 'generated' }).sign(privateKey);
      if (aud === profile.audience) assert.equal((await sdk.verify(token)).subject, 'user_a');
      else await assert.rejects(sdk.verify(token), IdentityFailure);
    }
  });
  testCases(['expired', 'backward', 'nonfinite'])('rejects %s clock across asynchronous key lookup', async (fault) => {
    const { privateKey, publicKey } = await generateKeyPair('RS256');
    let now = NOW;
    const sdk = createWorkOSProvider(profile, 'generated-api-key', {
      key: async () => { now = fault === 'expired' ? now + 2_000 : fault === 'backward' ? now - 2_000 : NaN; return publicKey; }, now: () => now,
    });
    const token = await new SignJWT({ iss: profile.issuer, aud: profile.audience,
      sub: 'user_a', sid: 'session_a', iat: NOW/1000-60, exp: NOW/1000+1, auth_time: NOW/1000-120 })
      .setProtectedHeader({ alg: 'RS256', kid: 'generated' }).sign(privateKey);
    await assert.rejects(sdk.verify(token), IdentityFailure);
  });
  testCases([
    [{ error: 'invalid_grant' }, 'denied'], [{ error: 'access_denied' }, 'denied'],
    [{ code: 'mfa_challenge' }, 'authentication_required'],
    [{ code: 'mfa_verification' }, 'authentication_required'],
    [{ code: 'sso_required' }, 'authentication_required'],
    [{ code: 'unrecognized_challenge' }, 'unknown'],
  ] as const)('classifies actual SDK error body %j', async (body, outcome) => {
    const transport = mock.fn(async () => new Response(JSON.stringify({ ...body,
      message: 'GENERATED_REFRESH_SENTINEL', authkit_redirect_url: 'https://foreign.invalid/' }),
      { status: 400, headers: { 'Content-Type': 'application/json' } }));
    const sdk = createWorkOSProvider(profile, 'generated-api-key', { fetch: transport });
    await assert.rejects(sdk.refresh(credentials, 'org_a'), { outcome });
    assert.equal(transport.mock.callCount(), 1);
  });
});

describe('distinct local and provider organization mappings', () => {
  testCases(['organizationId', 'identityIssuer', 'identityOrganizationId'] as const)('requires an explicit trusted target %s before contacting the provider', async (field) => {
      const f = fixture();
      const wrong = { ...targetA, [field]: field === 'identityOrganizationId' ? '' : 'foreign' };
      f.selections.authorize.mock.mockImplementation(async () => ({ target: wrong }));
      await assert.rejects(f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup'), /IDENTITY_MEMBERSHIP_CONFLICT/);
      assert.equal(f.provider.refresh.mock.callCount(), 0); assert.equal(f.selections.commit.mock.callCount(), 0);
    });
  it('never accepts the local organization ID as a provider JWT organization', async () => {
    const f = fixture(); f.provider.verify.mock.mockImplementation(async () => ({ ...claims, organizationId: 'org_a' }));
    assert.equal((await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup')).outcome, 'unknown');
    assert.equal(f.selections.commit.mock.callCount(), 0); assert.equal(f.session.reconciliationRequired, true);
  });
  testCases(['identityOrganizationId', 'membershipId', 'authorityRevision'] as const)('fences %s changed during the provider wait', async (field) => {
      const f = fixture(); const target = { ...targetA };
      f.selections.authorize.mock.mockImplementation(async () => ({ target }));
      f.provider.refresh.mock.mockImplementation(async () => { target[field] = 'changed'; return credentials; });
      assert.equal((await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup')).outcome, 'unknown');
      assert.ok(f.provider.refresh.mock.calls.some(call => isDeepStrictEqual(call.arguments, [credentials, 'identity_org_a'])));
      assert.equal(f.selections.commit.mock.callCount(), 0); assert.equal(f.session.reconciliationRequired, true);
    });
  it('binds an SSO continuation to both namespaces and commits only the local target', async () => {
    const f = fixture(); f.provider.refresh.mock.mockImplementation(async () => { throw new ProviderFailure('authentication_required'); });
    const outcome = await f.adapter.switchOrganization(f.request(), 'org_b', 0, '/setup');
    if (outcome.outcome !== 'authentication_required') throw new Error('Missing continuation');
    assert.ok(f.provider.authorizationURL.mock.calls.some(({ arguments: args }) => args.length === 3 && typeof args[0] === 'string' && typeof args[1] === 'string' && args[2] === 'identity_org_b'));
    const flow = [...f.flows.values()][0]!;
    assert.deepEqual(flow.selectionTarget, targetB);
    f.provider.verify.mock.mockImplementation(async () => ({ ...claims, organizationId: 'identity_org_b' }));
    const state = new URL(outcome.response.headers.get('location')!).searchParams.get('state');
    const cookie = outcome.response.headers.get('set-cookie')!.split(';')[0]!;
    const response = await f.adapter.callback(new Request(`${profile.callbackURI}?state=${state}&code=generated`, { headers: { cookie } }));
    assert.equal(response.status, 303);
    assert.deepEqual(f.selections.commit.mock.calls.map(({ arguments: args }) => args.slice(2)), [
      [intentFor('org_b'), targetB, { ...claims, organizationId: 'identity_org_b' }, stagedDigest],
    ]);
  });
  testCases(['before_exchange', 'during_exchange'] as const)('refuses changed continuation mapping %s', async (when) => {
    const f = fixture(); const target = { ...targetB };
    f.selections.authorize.mock.mockImplementation(async () => ({ target }));
    f.provider.refresh.mock.mockImplementation(async () => { throw new ProviderFailure('authentication_required'); });
    const outcome = await f.adapter.switchOrganization(f.request(), 'org_b', 0, '/setup');
    if (outcome.outcome !== 'authentication_required') throw new Error('Missing continuation');
    f.provider.verify.mock.mockImplementation(async () => ({ ...claims, organizationId: 'identity_org_b' }));
    if (when === 'before_exchange') target.identityOrganizationId = 'changed';
    else f.provider.exchange.mock.mockImplementation(async () => { target.identityOrganizationId = 'changed'; return credentials; });
    const state = new URL(outcome.response.headers.get('location')!).searchParams.get('state');
    const cookie = outcome.response.headers.get('set-cookie')!.split(';')[0]!;
    const response = await f.adapter.callback(new Request(`${profile.callbackURI}?state=${state}&code=generated`, { headers: { cookie } }));
    assert.equal(response.status, 409);
    if (when === 'before_exchange') assert.equal(f.provider.exchange.mock.callCount(), 0);
    assert.equal(f.selections.commit.mock.callCount(), 0);
    assert.equal(response.headers.getSetCookie().some((v) => v.startsWith('opentour-session=')), false);
    assert.equal(f.session.reconciliationRequired, true);
  });
});
