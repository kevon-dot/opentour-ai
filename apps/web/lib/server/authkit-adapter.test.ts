/** Generated providers and memory ports: simulated logic, not durable/provider acceptance. */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { exportJWK, generateKeyPair, SignJWT } from 'jose';
vi.mock('server-only', () => ({}));
import { createAuthkitAdapter, createWorkOSProvider, IdentityFailure, ProviderFailure,
  type AuthProfile, type AuthTransaction, type Claims, type Credentials, type ServerSession } from './authkit-adapter.js';

const NOW = Date.UTC(2026, 10, 15, 12);
const profile: AuthProfile = { origin: 'https://app.example.invalid',
  callbackURI: 'https://app.example.invalid/auth/callback', signedOutPath: '/signed-out',
  returnPaths: ['/', '/setup', '/signed-out'], providerOrigins: ['https://api.workos.com'],
  issuer: 'https://identity.example.invalid/', audience: 'https://api.example.invalid',
  jwksURL: 'https://api.workos.com/sso/jwks/client_fixture', clientId: 'client_fixture' };
const claims: Claims = { issuer: profile.issuer, subject: 'user_a', sessionId: 'session_a',
  organizationId: 'org_a', authenticationTime: new Date(NOW - 60_000).toISOString(), expiresAt: NOW + 600_000 };
const credentials: Credentials = { accessToken: 'GENERATED_ACCESS_SENTINEL',
  refreshToken: 'GENERATED_REFRESH_SENTINEL', userId: 'user_a' };

function fixture() {
  let currentTime = NOW;
  const flows = new Map<string, AuthTransaction>();
  const session: ServerSession = { handle: 'h'.repeat(43), ended: false, reconciliationRequired: false,
    csrfToken: 'csrf-fixture', credentials,
    identity: { actor: { kind: 'human', subject: claims.subject, issuer: claims.issuer },
      session_id: claims.sessionId, session_generation: 0,
      identity_checked_at: new Date(NOW).toISOString(), authentication_time: claims.authenticationTime,
      assurance_level: 'unknown' } };
  const sessions = {
    admit: vi.fn(async () => {
      if (session.ended) throw new IdentityFailure('SESSION_ENDED');
      return structuredClone(session);
    }),
    current: vi.fn(async () => structuredClone(session)),
    replaceCredentials: vi.fn(async (_handle: string, generation: number, next: Credentials) => {
      if (session.ended || generation !== session.identity.session_generation) throw new IdentityFailure('STALE_SESSION_CONTEXT');
      session.credentials = next;
    }),
    end: vi.fn(async () => {
      if (!session.ended) session.identity.session_generation++;
      session.ended = true;
      return { sessionId: session.identity.session_id, generation: session.identity.session_generation };
    }),
    requireReconciliation: vi.fn(async () => { session.reconciliationRequired = true; }),
  };
  const transactions = {
    create: vi.fn(async (flow: AuthTransaction) => { flows.set(`${flow.state}.${flow.nonce}`, structuredClone(flow)); }),
    consume: vi.fn(async (state: string, browser: string, now: number) => {
      const found = flows.get(state);
      if (!found || found.browserDigest !== browser || found.expiresAt <= now) throw new IdentityFailure('CALLBACK_INVALID');
      flows.delete(state);
      return structuredClone(found);
    }),
  };
  const provider = {
    authorizationURL: vi.fn(async (state: string) => `https://api.workos.com/user_management/authorize?state=${state}`),
    exchange: vi.fn(async () => credentials), refresh: vi.fn(async () => credentials),
    verify: vi.fn(async () => ({ ...claims })),
    logoutURL: vi.fn(() => 'https://api.workos.com/user_management/sessions/logout'),
  };
  const selections = { authorize: vi.fn(async () => undefined),
    commit: vi.fn(async () => { session.reconciliationRequired = false; }) };
  const adapter = createAuthkitAdapter({ profile, provider, transactions, sessions, selections, now: () => currentTime });
  const request = (csrf = true) => new Request(`${profile.origin}/auth/operation`, {
    method: 'POST', headers: { Cookie: `opentour-session=${session.handle}`, Origin: profile.origin,
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
  beforeEach(() => { vi.restoreAllMocks(); });
  it('allows public signin and commits before emitting a usable session cookie', async () => {
    const f = fixture(); const flow = await f.signin();
    expect(flow.response.status).toBe(303);
    expect(flow.response.headers.get('set-cookie')).toContain('Secure; HttpOnly; SameSite=Lax');
    expect(f.sessions.current).not.toHaveBeenCalled();
    const result = await f.adapter.callback(flow.callback);
    expect(result.status).toBe(303);
    expect(result.headers.get('location')).toBe(`${profile.origin}/setup`);
    expect(f.sessions.admit).toHaveBeenCalledTimes(1);
    expect(result.headers.get('set-cookie')).toContain('opentour-session=');
    expect(result.headers.get('cache-control')).toBe('no-store');
    expect(JSON.stringify([...result.headers])).not.toContain('SENTINEL');
  });
  it.each(['https://foreign.invalid', '//foreign.invalid', '/\\foreign', '/%2fforeign', '/not-allowed'])
    ('rejects unsafe return %s before creating a flow', async (path) => {
      const f = fixture(); const response = await f.adapter.signin(new Request(
        `${profile.origin}/auth/signin?return_to=${encodeURIComponent(path)}`));
      expect(response.status).toBe(422); expect(f.transactions.create).not.toHaveBeenCalled();
    });
  it('ignores forwarded redirect configuration', async () => {
    const f = fixture(); const result = await f.adapter.signin(new Request(`${profile.origin}/auth/signin`,
      { headers: { 'x-redirect-uri': 'https://foreign.invalid', 'x-forwarded-host': 'foreign.invalid' } }));
    expect(result.status).toBe(303); expect(result.headers.get('location')).not.toContain('foreign');
  });
  it('consumes once across two concurrent callback attempts', async () => {
    const f = fixture(); const flow = await f.signin();
    const results = await Promise.all([f.adapter.callback(flow.callback), f.adapter.callback(flow.callback)]);
    expect(results.map((r) => r.status).sort()).toEqual([303, 401]);
    expect(f.provider.exchange).toHaveBeenCalledTimes(1);
  });
  it.each(['foreign-browser', 'expired', 'duplicate-state', 'duplicate-code', 'nonce'])
    ('refuses callback %s before code exchange', async (fault) => {
      const f = fixture(); const flow = await f.signin(); let req = flow.callback;
      if (fault === 'expired') f.advance(600_000);
      if (fault === 'foreign-browser') req = new Request(req.url, { headers: { Cookie: `opentour-auth-flow=${'z'.repeat(43)}` } });
      if (fault === 'duplicate-state') req = new Request(req.url + '&state=duplicate', { headers: req.headers });
      if (fault === 'duplicate-code') req = new Request(req.url + '&code=duplicate', { headers: req.headers });
      if (fault === 'nonce') req = new Request(req.url.replace(flow.state, flow.state.slice(0, 44) + 'z'.repeat(43)), { headers: req.headers });
      expect((await f.adapter.callback(req)).status).toBe(401);
      expect(f.provider.exchange).not.toHaveBeenCalled(); expect(f.sessions.admit).not.toHaveBeenCalled();
    });
  it.each(['claim', 'admission', 'provider'])('does not mint success for unknown %s outcome', async (fault) => {
    const f = fixture(); const flow = await f.signin();
    const error = new Error('GENERATED_REFRESH_SENTINEL');
    if (fault === 'claim') f.transactions.consume.mockRejectedValue(error);
    if (fault === 'admission') f.sessions.admit.mockRejectedValue(error);
    if (fault === 'provider') f.provider.exchange.mockRejectedValue(error);
    const log = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const response = await f.adapter.callback(flow.callback);
    expect(response.status).toBe(503); expect(response.headers.get('set-cookie')).not.toContain('opentour-session=');
    expect(await response.text()).not.toContain('SENTINEL'); expect(log).not.toHaveBeenCalled();
  });
  it('checks callback expiry again after provider exchange', async () => {
    const f = fixture(); const flow = await f.signin();
    f.provider.exchange.mockImplementation(async () => { f.advance(600_000); return credentials; });
    expect((await f.adapter.callback(flow.callback)).status).toBe(401);
    expect(f.sessions.admit).not.toHaveBeenCalled();
  });
  it('callback never invokes invitation or selection for ordinary signin', async () => {
    const f = fixture(); const flow = await f.signin(); await f.adapter.callback(flow.callback);
    expect(f.selections.commit).not.toHaveBeenCalled(); expect(f.selections.authorize).not.toHaveBeenCalled();
  });
  it.each(['ended', 'generation'])('rechecks %s after deferred token verification', async (change) => {
    const f = fixture(); f.provider.verify.mockImplementation(async () => {
      if (change === 'ended') f.session.ended = true; else f.session.identity.session_generation++;
      return claims;
    });
    await expect(f.adapter.verifiedIdentity(f.request(), 'session_context')).rejects.toBeInstanceOf(IdentityFailure);
  });
  it('unscoped identity exposes no tenant authority during reconciliation', async () => {
    const f = fixture(); f.session.reconciliationRequired = true;
    const identity = await f.adapter.verifiedIdentity(f.request(), 'session_context');
    expect(identity.assurance_level).toBe('unknown'); expect('organization_id' in identity).toBe(false);
    await expect(f.adapter.verifiedIdentity(f.request(), 'tenant_read' as never)).rejects.toBeInstanceOf(IdentityFailure);
  });
});

describe('ending and switching', () => {
  it('confirms only the exact verified target after current membership and local commit', async () => {
    const f = fixture();
    expect((await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup')).outcome).toBe('verified_target');
    expect(f.selections.authorize).toHaveBeenCalledTimes(2);
    expect(f.selections.commit).toHaveBeenCalledExactlyOnceWith(f.session.handle, 0, 'org_a', claims);
    expect(f.session.reconciliationRequired).toBe(false);
  });
  it('requires a durable pending marker before any provider context mutation', async () => {
    const f = fixture(); f.sessions.requireReconciliation.mockRejectedValue(new Error('unknown local state'));
    await expect(f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup')).rejects.toThrow();
    expect(f.provider.refresh).not.toHaveBeenCalled(); expect(f.selections.commit).not.toHaveBeenCalled();
  });
  it('checks CSRF before mutation', async () => {
    const f = fixture(); expect((await f.adapter.signout(f.request(false))).status).toBe(403);
    expect(f.sessions.end).not.toHaveBeenCalled();
  });
  it('provider logout failure cannot undo committed local denial', async () => {
    const f = fixture(); f.provider.logoutURL.mockImplementation(() => { throw new Error('SECRET'); });
    const result = await f.adapter.signout(f.request());
    expect(result.status).toBe(200); expect(f.session.ended).toBe(true);
    expect(await result.json()).toEqual({ local_ended: true, provider_completion: 'unconfirmed' });
  });
  it('unknown local denial emits no success cookie or redirect', async () => {
    const f = fixture(); f.sessions.end.mockRejectedValue(new Error('SECRET'));
    const result = await f.adapter.signout(f.request());
    expect(result.status).toBe(503); expect(result.headers.get('location')).toBeNull();
    expect(result.headers.get('set-cookie')).toBeNull(); expect(f.provider.logoutURL).not.toHaveBeenCalled();
  });
  it.each(['subject', 'sessionId', 'organizationId'])('rejects changed refresh %s', async (field) => {
    const f = fixture(); f.provider.verify.mockResolvedValue({ ...claims, [field]: 'foreign' });
    expect((await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup')).outcome).toBe('unknown');
    expect(f.selections.commit).not.toHaveBeenCalled(); expect(f.session.reconciliationRequired).toBe(true);
  });
  it('logout racing refresh cannot write credentials or select', async () => {
    const f = fixture(); f.provider.refresh.mockImplementation(async () => { f.session.ended = true; return credentials; });
    expect((await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup')).outcome).toBe('unknown');
    expect(f.sessions.replaceCredentials).not.toHaveBeenCalled(); expect(f.selections.commit).not.toHaveBeenCalled();
  });
  it('provider refresh and failed local commit remain separately unknown', async () => {
    const f = fixture(); f.selections.commit.mockRejectedValue(new Error('commit ack lost'));
    expect((await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup')).outcome).toBe('unknown');
    expect(f.session.reconciliationRequired).toBe(true); expect(f.session.ended).toBe(false);
  });
  it.each(['denied', 'transient', 'unknown'] as const)('preserves provider %s without false logout', async (outcome) => {
    const f = fixture(); f.provider.refresh.mockRejectedValue(new ProviderFailure(outcome));
    expect((await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup')).outcome).toBe(outcome);
    expect(f.session.ended).toBe(false); expect(f.selections.commit).not.toHaveBeenCalled();
  });
  it('creates a bound SSO continuation without trusting arbitrary provider URLs', async () => {
    const f = fixture(); f.provider.refresh.mockRejectedValue(new ProviderFailure('authentication_required'));
    const outcome = await f.adapter.switchOrganization(f.request(), 'org_b', 0, '/setup');
    expect(outcome.outcome).toBe('authentication_required');
    const flow = [...f.flows.values()][0]!;
    expect([flow.targetOrganization, flow.sessionId, flow.subject, flow.generation]).toEqual(['org_b','session_a','user_a',0]);
    expect(flow.browserDigest).toHaveLength(64); expect(flow.nonce).toHaveLength(43);
  });
  it('rejects an SSO callback after another selection advances the generation', async () => {
    const f = fixture(); f.provider.refresh.mockRejectedValue(new ProviderFailure('authentication_required'));
    const outcome = await f.adapter.switchOrganization(f.request(), 'org_b', 0, '/setup');
    if (outcome.outcome !== 'authentication_required') throw new Error('Missing continuation');
    const state = new URL(outcome.response.headers.get('location')!).searchParams.get('state');
    const cookie = outcome.response.headers.get('set-cookie')!.split(';')[0]!;
    f.session.identity.session_generation++;
    const response = await f.adapter.callback(new Request(`${profile.callbackURI}?state=${state}&code=generated`,
      { headers: { cookie } }));
    expect(response.status).toBe(409); expect(f.provider.exchange).not.toHaveBeenCalled();
    expect(f.selections.commit).not.toHaveBeenCalled();
  });
  it('rechecks local membership after provider refresh and before selection', async () => {
    const f = fixture();
    f.selections.authorize.mockResolvedValueOnce(undefined).mockRejectedValueOnce(new IdentityFailure('MEMBERSHIP_INACTIVE', 403));
    expect((await f.adapter.switchOrganization(f.request(), 'org_a', 0, '/setup')).outcome).toBe('unknown');
    expect(f.selections.commit).not.toHaveBeenCalled(); expect(f.session.reconciliationRequired).toBe(true);
  });
});

describe('actual maintained SDK and JWT boundary with generated inputs', () => {
  it('bounds received streaming bytes and aborts the provider body', async () => {
    let observedSignal: AbortSignal | undefined;
    const cancelled = vi.fn();
    const sdk = createWorkOSProvider(profile, 'generated-api-key', { fetch: async (_input, init) => {
      expect(init?.redirect).toBe('error'); observedSignal = init?.signal ?? undefined;
      return new Response(new ReadableStream({ start(controller) {
        controller.enqueue(new Uint8Array(40_000)); controller.enqueue(new Uint8Array(40_000));
      }, cancel: cancelled }));
    } });
    await expect(sdk.exchange('generated-code', 'verifier')).rejects.toBeInstanceOf(ProviderFailure);
    expect(observedSignal?.aborted).toBe(true); expect(cancelled).toHaveBeenCalledOnce();
  });
  it('applies its five-second deadline to a body that never finishes', async () => {
    vi.useFakeTimers();
    const cancelled = vi.fn();
    try {
      const sdk = createWorkOSProvider(profile, 'generated-api-key', { fetch: async () =>
        new Response(new ReadableStream({ cancel: cancelled })) });
      const operation = expect(sdk.exchange('generated-code', 'verifier')).rejects.toBeInstanceOf(ProviderFailure);
      await vi.advanceTimersByTimeAsync(5_001);
      await operation; expect(cancelled).toHaveBeenCalledOnce();
    } finally { vi.useRealTimers(); }
  });
  it('keeps only unexpired cached keys usable during one unknown-key refresh', async () => {
    const { publicKey, privateKey } = await generateKeyPair('RS256');
    const jwk = { ...await exportJWK(publicKey), kid: 'known', alg: 'RS256' };
    const document = { keys: [jwk] };
    let monotonic = 0;
    const clock = vi.spyOn(performance, 'now').mockImplementation(() => monotonic);
    let release: (response: Response) => void = () => undefined;
    const transport = vi.fn(async () => new Response(JSON.stringify(document)));
    const sdk = createWorkOSProvider(profile, 'generated-api-key', { fetch: transport, now: () => NOW });
    const token = (kid: string) => new SignJWT({ iss: profile.issuer, aud: profile.audience,
      sub: 'user_a', sid: 'session_a', iat: NOW/1000-60, exp: NOW/1000+600, auth_time: NOW/1000-120 })
      .setProtectedHeader({ alg: 'RS256', kid }).sign(privateKey);
    try {
      const known = await token('known'); await sdk.verify(known);
      monotonic = 31_000;
      transport.mockImplementationOnce(async () => new Promise<Response>((resolve) => { release = resolve; }));
      const pending = expect(sdk.verify(await token('unknown'))).rejects.toBeInstanceOf(IdentityFailure);
      await Promise.resolve();
      expect((await sdk.verify(known)).subject).toBe('user_a');
      monotonic = 300_001;
      await expect(sdk.verify(known)).rejects.toMatchObject({ status: 503 });
      release(new Response(JSON.stringify(document))); await pending;
      expect(transport).toHaveBeenCalledTimes(2);
    } finally { clock.mockRestore(); }
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
    expect(url.searchParams.get('code_challenge_method')).toBe('S256');
    expect(url.searchParams.get('code_challenge')).toHaveLength(43);
    expect(url.searchParams.get('state')).toBe('bound-state-and-nonce');
    expect(url.href).not.toContain(verifier);
    await expect(sdk.exchange('generated-code', verifier)).rejects.toMatchObject({ outcome: 'denied' });
    expect(JSON.parse(sentBody).code_verifier).toBe(verifier);
  });
  it('does not retry or leak a credential-bearing SDK mutation fault', async () => {
    const transport = vi.fn(async () => new Response(JSON.stringify({ message: 'GENERATED_REFRESH_SENTINEL' }),
      { status: 500, headers: { 'Content-Type': 'application/json' } }));
    const sdk = createWorkOSProvider(profile, 'generated-api-key', { fetch: transport });
    let error: unknown;
    try { await sdk.exchange('generated-code', 'verifier'); } catch (caught) { error = caught; }
    expect(error).toBeInstanceOf(ProviderFailure); expect(String(error)).not.toContain('SENTINEL');
    expect(transport).toHaveBeenCalledTimes(1);
  });
  it('requires signed audience and current claims', async () => {
    const { privateKey, publicKey } = await generateKeyPair('RS256');
    const sdk = createWorkOSProvider(profile, 'generated-api-key', { key: async () => publicKey, now: () => NOW });
    for (const aud of [profile.audience, 'wrong', undefined]) {
      const payload = { iss: profile.issuer, ...(aud ? { aud } : {}), sub: 'user_a', sid: 'session_a',
        iat: NOW/1000-60, exp: NOW/1000+60, auth_time: NOW/1000-120 };
      const token = await new SignJWT(payload).setProtectedHeader({ alg: 'RS256', kid: 'generated' }).sign(privateKey);
      if (aud === profile.audience) expect((await sdk.verify(token)).subject).toBe('user_a');
      else await expect(sdk.verify(token)).rejects.toBeInstanceOf(IdentityFailure);
    }
  });
  it.each(['expired', 'backward', 'nonfinite'])('rejects %s clock across asynchronous key lookup', async (fault) => {
    const { privateKey, publicKey } = await generateKeyPair('RS256');
    let now = NOW;
    const sdk = createWorkOSProvider(profile, 'generated-api-key', {
      key: async () => { now = fault === 'expired' ? now + 2_000 : fault === 'backward' ? now - 2_000 : NaN; return publicKey; }, now: () => now,
    });
    const token = await new SignJWT({ iss: profile.issuer, aud: profile.audience,
      sub: 'user_a', sid: 'session_a', iat: NOW/1000-60, exp: NOW/1000+1, auth_time: NOW/1000-120 })
      .setProtectedHeader({ alg: 'RS256', kid: 'generated' }).sign(privateKey);
    await expect(sdk.verify(token)).rejects.toBeInstanceOf(IdentityFailure);
  });
  it.each([
    [{ error: 'invalid_grant' }, 'denied'], [{ error: 'access_denied' }, 'denied'],
    [{ code: 'mfa_challenge' }, 'authentication_required'],
    [{ code: 'mfa_verification' }, 'authentication_required'],
    [{ code: 'sso_required' }, 'authentication_required'],
    [{ code: 'unrecognized_challenge' }, 'unknown'],
  ])('classifies actual SDK error body %j', async (body, outcome) => {
    const transport = vi.fn(async () => new Response(JSON.stringify({ ...body,
      message: 'GENERATED_REFRESH_SENTINEL', authkit_redirect_url: 'https://foreign.invalid/' }),
      { status: 400, headers: { 'Content-Type': 'application/json' } }));
    const sdk = createWorkOSProvider(profile, 'generated-api-key', { fetch: transport });
    await expect(sdk.refresh(credentials, 'org_a')).rejects.toMatchObject({ outcome });
    expect(transport).toHaveBeenCalledTimes(1);
  });
});
