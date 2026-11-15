import 'server-only';
import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { WorkOS } from '@workos-inc/node';
import { createRemoteJWKSet, decodeProtectedHeader, jwtVerify, type JWTVerifyGetKey } from 'jose';

/** Backend ports only. OPE-297 composes routes; OPE-172 owns selection intent/CAS. */
export type Identity = {
  actor: { kind: 'human'; subject: string; issuer: string };
  session_id: string; session_generation: number; identity_checked_at: string;
  authentication_time: string; assurance_level: 'unknown';
};
export type IdentityUse = 'organization_create' | 'own_organizations' | 'recipient_invitation' |
  'own_setup_operation' | 'account_entry' | 'session_end' | 'session_context';
const identityUses: readonly string[] = ['organization_create', 'own_organizations', 'recipient_invitation',
  'own_setup_operation', 'account_entry', 'session_end', 'session_context'];
export type Claims = {
  issuer: string; subject: string; sessionId: string; organizationId: string | null;
  authenticationTime: string; expiresAt: number;
};
export type Credentials = { accessToken: string; refreshToken: string; userId: string };
export type ServerSession = {
  handle: string; identity: Identity; credentials: Credentials; csrfToken: string;
  ended: boolean;
  reconciliationRequired: boolean;
};
export type AuthTransaction = {
  state: string; nonce: string; browserDigest: string; verifier: string; expiresAt: number;
  returnPath: string; kind: 'signin' | 'switch';
  sessionHandle: string | null; subject: string | null; sessionId: string | null;
  generation: number | null; targetOrganization: string | null;
};
export interface AuthTransactionPort {
  /** Durably store encrypted private state, TTL<=600s; no in-memory fallback. */
  create(transaction: AuthTransaction): Promise<void>;
  /** Atomic single-use claim. Refused/unknown commit throws before provider exchange. */
  consume(stateAndNonce: string, browserDigest: string, now: number): Promise<AuthTransaction>;
}
export interface SessionPort {
  /** Commit strict verified identity + opaque binding + sealed credentials before returning. */
  admit(credentials: Credentials, claims: Claims): Promise<ServerSession>;
  current(handle: string): Promise<ServerSession>;
  /** Compare session/subject/generation and ended marker in the credential-write transaction. */
  replaceCredentials(handle: string, generation: number, credentials: Credentials,
    claims: Claims): Promise<void>;
  /** Durable idempotent ended marker + generation increment; uncertain commit must throw. */
  end(handle: string, generation: number): Promise<{ sessionId: string; generation: number }>;
  /** Mark private-context admission unavailable without ending the valid human identity. */
  requireReconciliation(handle: string, generation: number): Promise<void>;
}
export interface SelectionPort {
  /** Current local active membership/freshness, before provider preparation and at local CAS. */
  authorize(identity: Identity, targetOrganization: string): Promise<void>;
  commit(handle: string, observedGeneration: number, targetOrganization: string,
    claims: Claims): Promise<void>;
}
export interface ProviderPort {
  authorizationURL(state: string, verifier: string, targetOrganization?: string): Promise<string>;
  exchange(code: string, verifier: string): Promise<Credentials>;
  refresh(credentials: Credentials, targetOrganization: string): Promise<Credentials>;
  verify(accessToken: string): Promise<Claims>;
  logoutURL(sessionId: string): string;
}
export type AuthProfile = {
  origin: string; callbackURI: string; signedOutPath: string; returnPaths: readonly string[];
  providerOrigins: readonly string[]; issuer: string; audience: string; jwksURL: string;
  clientId: string;
};

export class IdentityFailure extends Error {
  constructor(readonly code: string, readonly status = 401) { super(code); }
}
export class ProviderFailure extends Error {
  constructor(readonly outcome: 'authentication_required' | 'denied' | 'transient' | 'unknown') {
    super(outcome);
  }
}
const sessionCookie = 'opentour-session';
const flowCookie = 'opentour-auth-flow';
const sha = (value: string): string => createHash('sha256').update(value).digest('hex');
const secret = (): string => randomBytes(32).toString('base64url');
const validId = (value: unknown): value is string =>
  typeof value === 'string' && value.length > 0 && value.length <= 256;
const validGeneration = (value: number): boolean => Number.isSafeInteger(value) && value >= 0;

function cookie(request: Request, name: string): string {
  const entries = (request.headers.get('cookie') ?? '').split(';').map((v) => v.trim());
  const values = entries.filter((v) => v.startsWith(`${name}=`)).map((v) => v.slice(name.length + 1));
  if (values.length !== 1 || !/^[A-Za-z0-9_-]{32,128}$/.test(values[0] ?? '')) {
    throw new IdentityFailure('SESSION_INVALID');
  }
  return values[0]!;
}
function setCookie(name: string, value: string, maxAge?: number): string {
  return `${name}=${value}; Path=/; Secure; HttpOnly; SameSite=Lax${maxAge === undefined ? '' : `; Max-Age=${maxAge}`}`;
}
function noStore(): Headers {
  return new Headers({ 'Cache-Control': 'no-store', Vary: 'Cookie', 'Referrer-Policy': 'no-referrer' });
}
function safeError(error: unknown, clearFlow = false): Response {
  const failure = error instanceof IdentityFailure ? error : new IdentityFailure('IDENTITY_UNAVAILABLE', 503);
  const headers = noStore();
  headers.set('Content-Type', 'application/json');
  if (clearFlow) headers.append('Set-Cookie', setCookie(flowCookie, '', 0));
  return new Response(JSON.stringify({ code: failure.code,
    message: 'The identity operation could not be completed.', request_id: secret(),
    retryable: failure.status === 503, field_errors: [], current_revision: null }),
  { status: failure.status, headers });
}
function redirect(url: string, headers = noStore()): Response {
  headers.set('Location', url);
  return new Response(null, { status: 303, headers });
}
function profileCheck(profile: AuthProfile): void {
  for (const raw of [profile.origin, profile.callbackURI, profile.issuer, profile.audience,
    profile.jwksURL, ...profile.providerOrigins]) {
    const url = new URL(raw);
    if (url.protocol !== 'https:' || url.username || url.password || url.hash) {
      throw new IdentityFailure('IDENTITY_CONFIGURATION_INVALID', 503);
    }
  }
  if (new URL(profile.origin).origin !== profile.origin || !validId(profile.clientId)
      || new URL(profile.callbackURI).origin !== profile.origin
      || !profile.returnPaths.includes(profile.signedOutPath)) {
    throw new IdentityFailure('IDENTITY_CONFIGURATION_INVALID', 503);
  }
  for (const path of profile.returnPaths) checkedReturn(profile, path);
}
function checkedReturn(profile: AuthProfile, path: string): string {
  if (!path.startsWith('/') || path.startsWith('//') || /[\\\r\n%]/.test(path)
      || !profile.returnPaths.includes(path) || new URL(path, profile.origin).origin !== profile.origin) {
    throw new IdentityFailure('RETURN_DESTINATION_INVALID', 422);
  }
  return path;
}
function providerDestination(profile: AuthProfile, raw: string): string {
  const url = new URL(raw);
  if (url.protocol !== 'https:' || url.username || url.password || url.hash
      || !profile.providerOrigins.includes(url.origin)) {
    throw new IdentityFailure('PROVIDER_DESTINATION_INVALID', 503);
  }
  return raw;
}
function mutation(request: Request, profile: AuthProfile, session: ServerSession): void {
  const received = request.headers.get('x-csrf-token') ?? '';
  const expected = session.csrfToken;
  if (request.headers.get('origin') !== profile.origin || !expected || received.length > 256
      || Buffer.byteLength(received) !== Buffer.byteLength(expected)
      || !timingSafeEqual(Buffer.from(received), Buffer.from(expected))) {
    throw new IdentityFailure('CSRF_INVALID', 403);
  }
}
function active(session: ServerSession, generation?: number): void {
  if (session.ended || !validGeneration(session.identity.session_generation)) {
    throw new IdentityFailure('SESSION_ENDED');
  }
  if (generation !== undefined && (!validGeneration(generation)
      || session.identity.session_generation !== generation)) {
    throw new IdentityFailure('STALE_SESSION_CONTEXT', 409);
  }
}
function sameSession(session: ServerSession, claims: Claims, target?: string): void {
  if (claims.subject !== session.identity.actor.subject || claims.sessionId !== session.identity.session_id
      || claims.issuer !== session.identity.actor.issuer
      || (target !== undefined && claims.organizationId !== target)) {
    throw new IdentityFailure('SESSION_INVALID');
  }
}

export function createWorkOSProvider(profile: AuthProfile, apiKey: string,
  options: { fetch?: typeof fetch; key?: JWTVerifyGetKey; now?: () => number } = {}): ProviderPort {
  profileCheck(profile);
  const now = options.now ?? Date.now;
  const sdk = new WorkOS(apiKey, { clientId: profile.clientId, issuer: profile.issuer,
    timeout: 5_000, maxRetries: 0, fetchFn: options.fetch });
  const jwks = options.key ?? createRemoteJWKSet(new URL(profile.jwksURL), {
    timeoutDuration: 5_000, cooldownDuration: 30_000, cacheMaxAge: 300_000,
  });
  const classify = (error: unknown): ProviderFailure => {
    const e = error as { code?: unknown; error?: unknown; status?: unknown };
    if (['sso_required', 'mfa_enrollment', 'mfa_challenge', 'mfa_verification'].includes(String(e?.code))) {
      return new ProviderFailure('authentication_required');
    }
    if (['invalid_grant', 'access_denied'].includes(String(e?.error))
        || e?.code === 'organization_membership_not_found') {
      return new ProviderFailure('denied');
    }
    if (e?.status === 429) return new ProviderFailure('transient');
    // A transport/5xx failure after a mutation can hide acceptance. Do not replay it.
    return new ProviderFailure('unknown');
  };
  return {
    async authorizationURL(state, verifier, targetOrganization) {
      const challenge = createHash('sha256').update(verifier).digest('base64url');
      return providerDestination(profile, sdk.userManagement.getAuthorizationUrl({
        provider: 'authkit', clientId: profile.clientId, redirectUri: profile.callbackURI,
        state, codeChallenge: challenge, codeChallengeMethod: 'S256', organizationId: targetOrganization,
      }));
    },
    async exchange(code, verifier) {
      try {
        const result = await sdk.userManagement.authenticateWithCode({ clientId: profile.clientId,
          code, codeVerifier: verifier });
        return { accessToken: result.accessToken, refreshToken: result.refreshToken, userId: result.user.id };
      } catch (error) { throw classify(error); }
    },
    async refresh(credentials, targetOrganization) {
      try {
        const result = await sdk.userManagement.authenticateWithRefreshToken({ clientId: profile.clientId,
          refreshToken: credentials.refreshToken, organizationId: targetOrganization });
        return { accessToken: result.accessToken, refreshToken: result.refreshToken, userId: result.user.id };
      } catch (error) { throw classify(error); }
    },
    async verify(token) {
      try {
        if (typeof token !== 'string' || token.length > 16_384) throw new Error();
        const header = decodeProtectedHeader(token);
        if (!validId(header.kid) || header.alg !== 'RS256' || header.jku || header.jwk || header.x5u) throw new Error();
        const { payload } = await jwtVerify(token, jwks, { issuer: profile.issuer,
          audience: profile.audience, algorithms: ['RS256'], clockTolerance: 0,
          currentDate: new Date(now()), requiredClaims: ['iss', 'aud', 'sub', 'sid', 'iat', 'exp', 'auth_time'] });
        if (!validId(payload.sub) || !validId(payload.sid) || payload.aud !== profile.audience
            || !Number.isSafeInteger(payload.auth_time) || !Number.isSafeInteger(payload.iat)
            || !Number.isSafeInteger(payload.exp) || (payload.auth_time as number) < 0
            || (payload.auth_time as number) > payload.iat! || payload.iat! > now() / 1000
            || payload.exp! <= payload.iat! || (payload.sub_profile !== undefined && payload.sub_profile !== 'user')
            || payload.sub_type !== undefined || (payload.org_id !== undefined && !validId(payload.org_id))) throw new Error();
        return { issuer: profile.issuer, subject: payload.sub, sessionId: payload.sid,
          organizationId: typeof payload.org_id === 'string' ? payload.org_id : null,
          authenticationTime: new Date((payload.auth_time as number) * 1000).toISOString(),
          expiresAt: payload.exp! * 1000 };
      } catch { throw new IdentityFailure('SESSION_INVALID'); }
    },
    logoutURL(sessionId) {
      return providerDestination(profile, sdk.userManagement.getLogoutUrl({ sessionId,
        returnTo: new URL(profile.signedOutPath, profile.origin).href }));
    },
  };
}

export function createAuthkitAdapter(ports: {
  profile: AuthProfile; provider: ProviderPort; transactions: AuthTransactionPort;
  sessions: SessionPort; selections: SelectionPort; now?: () => number;
}) {
  const { profile, provider, transactions, sessions, selections } = ports;
  profileCheck(profile);
  const now = ports.now ?? Date.now;

  async function start(returnPath: string, browser: string, session?: ServerSession,
    targetOrganization?: string): Promise<Response> {
    checkedReturn(profile, returnPath);
    const transaction: AuthTransaction = { state: secret(), nonce: secret(), browserDigest: sha(browser),
      verifier: secret(), expiresAt: now() + 600_000, returnPath,
      kind: session ? 'switch' : 'signin', sessionHandle: session?.handle ?? null,
      subject: session?.identity.actor.subject ?? null, sessionId: session?.identity.session_id ?? null,
      generation: session?.identity.session_generation ?? null, targetOrganization: targetOrganization ?? null };
    const location = providerDestination(profile, await provider.authorizationURL(
      `${transaction.state}.${transaction.nonce}`, transaction.verifier, targetOrganization));
    await transactions.create(transaction);
    const headers = noStore();
    headers.append('Set-Cookie', setCookie(flowCookie, browser, 600));
    return redirect(location, headers);
  }
  return {
    async signin(request: Request): Promise<Response> {
      try {
        const url = new URL(request.url);
        if (url.searchParams.getAll('return_to').length > 1
            || [...url.searchParams.keys()].some((key) => key !== 'return_to')) {
          throw new IdentityFailure('SCHEMA_INVALID', 422);
        }
        return await start(url.searchParams.get('return_to') ?? '/', secret());
      } catch (error) { return safeError(error); }
    },
    async callback(request: Request): Promise<Response> {
      let pendingSwitch: { handle: string; generation: number } | undefined;
      try {
        const url = new URL(request.url);
        const stateValues = url.searchParams.getAll('state');
        const codeValues = url.searchParams.getAll('code');
        if (stateValues.length !== 1 || codeValues.length !== 1
            || [...url.searchParams.keys()].some((key) => !['state', 'code'].includes(key))
            || !/^[A-Za-z0-9_-]{43}\.[A-Za-z0-9_-]{43}$/.test(stateValues[0]!)
            || !codeValues[0] || codeValues[0].length > 4096) throw new IdentityFailure('CALLBACK_INVALID');
        const [state, nonce] = stateValues[0]!.split('.');
        const browser = cookie(request, flowCookie);
        const transaction = await transactions.consume(stateValues[0]!, sha(browser), now());
        if (transaction.state !== state || transaction.nonce !== nonce
            || transaction.browserDigest !== sha(browser) || transaction.expiresAt <= now()) {
          throw new IdentityFailure('CALLBACK_INVALID');
        }
        checkedReturn(profile, transaction.returnPath);
        let previous: ServerSession | undefined;
        if (transaction.kind === 'switch') {
          if (!transaction.sessionHandle || transaction.generation === null || !transaction.targetOrganization) {
            throw new IdentityFailure('CALLBACK_INVALID');
          }
          previous = await sessions.current(transaction.sessionHandle);
          pendingSwitch = { handle: previous.handle, generation: transaction.generation };
          active(previous, transaction.generation);
          if (previous.identity.actor.subject !== transaction.subject
              || previous.identity.session_id !== transaction.sessionId) throw new IdentityFailure('CALLBACK_INVALID');
          await selections.authorize(previous.identity, transaction.targetOrganization);
        }
        // Durable claim is already consumed. Lost exchange outcome cannot automatically retry the code.
        const credentials = await provider.exchange(codeValues[0], transaction.verifier);
        const claims = await provider.verify(credentials.accessToken);
        if (transaction.expiresAt <= now()) throw new IdentityFailure('CALLBACK_INVALID');
        if (claims.subject !== credentials.userId) throw new IdentityFailure('SESSION_INVALID');
        let session: ServerSession;
        if (previous) {
          sameSession(previous, claims, transaction.targetOrganization!);
          active(await sessions.current(previous.handle), transaction.generation!);
          await sessions.replaceCredentials(previous.handle, transaction.generation!, credentials, claims);
          await selections.authorize(previous.identity, transaction.targetOrganization!);
          await selections.commit(previous.handle, transaction.generation!, transaction.targetOrganization!, claims);
          session = await sessions.current(previous.handle);
        } else {
          session = await sessions.admit(credentials, claims);
        }
        active(session);
        sameSession(session, claims);
        const headers = noStore();
        headers.append('Set-Cookie', setCookie(flowCookie, '', 0));
        headers.append('Set-Cookie', setCookie(sessionCookie, session.handle));
        return redirect(new URL(transaction.returnPath, profile.origin).href, headers);
      } catch (error) {
        if (pendingSwitch) {
          try { await sessions.requireReconciliation(pendingSwitch.handle, pendingSwitch.generation); }
          catch { /* Unknown local commit is still a failed callback, never authenticated success. */ }
        }
        return safeError(error, true);
      }
    },
    async signout(request: Request): Promise<Response> {
      try {
        const session = await sessions.current(cookie(request, sessionCookie));
        mutation(request, profile, session);
        let ended: { sessionId: string; generation: number };
        try { ended = await sessions.end(session.handle, session.identity.session_generation); }
        catch { throw new IdentityFailure('SESSION_END_UNCONFIRMED', 503); }
        const headers = noStore();
        headers.append('Set-Cookie', setCookie(sessionCookie, '', 0));
        try { return redirect(providerDestination(profile, provider.logoutURL(ended.sessionId)), headers); }
        catch {
          // This explicit outcome is consumed by297; it does not claim remote logout completion.
          headers.set('Content-Type', 'application/json');
          return new Response(JSON.stringify({ local_ended: true, provider_completion: 'unconfirmed' }),
            { status: 200, headers });
        }
      } catch (error) { return safeError(error); }
    },
    async switchOrganization(request: Request, targetOrganization: string,
      observedGeneration: number, returnPath: string): Promise<
      { outcome: 'verified_target' | 'denied' | 'transient' | 'unknown' } |
      { outcome: 'authentication_required'; response: Response }> {
      const session = await sessions.current(cookie(request, sessionCookie));
      mutation(request, profile, session);
      active(session, observedGeneration);
      checkedReturn(profile, returnPath);
      if (!validId(targetOrganization)) throw new IdentityFailure('SCHEMA_INVALID', 422);
      await selections.authorize(session.identity, targetOrganization);
      let credentials: Credentials;
      try { credentials = await provider.refresh(session.credentials, targetOrganization); }
      catch (error) {
        if (error instanceof ProviderFailure) {
          if (error.outcome === 'authentication_required') {
            return { outcome: error.outcome, response: await start(returnPath, secret(), session, targetOrganization) };
          }
          if (error.outcome === 'unknown') await sessions.requireReconciliation(session.handle, observedGeneration);
          return { outcome: error.outcome };
        }
        await sessions.requireReconciliation(session.handle, observedGeneration);
        return { outcome: 'unknown' };
      }
      try {
        const claims = await provider.verify(credentials.accessToken);
        sameSession(session, claims, targetOrganization);
        if (credentials.userId !== claims.subject) throw new IdentityFailure('SESSION_INVALID');
        active(await sessions.current(session.handle), observedGeneration);
        await sessions.replaceCredentials(session.handle, observedGeneration, credentials, claims);
        await selections.authorize(session.identity, targetOrganization);
        await selections.commit(session.handle, observedGeneration, targetOrganization, claims);
        return { outcome: 'verified_target' };
      } catch {
        await sessions.requireReconciliation(session.handle, observedGeneration);
        return { outcome: 'unknown' };
      }
    },
    async verifiedIdentity(request: Request, use: IdentityUse): Promise<Identity> {
      if (!identityUses.includes(use)) throw new IdentityFailure('CAPABILITY_DENIED', 403);
      const session = await sessions.current(cookie(request, sessionCookie));
      active(session);
      const claims = await provider.verify(session.credentials.accessToken);
      sameSession(session, claims);
      const current = await sessions.current(session.handle);
      active(current, session.identity.session_generation);
      sameSession(current, claims);
      // Unscoped verified identity is allowed for reconciliation; tenant context is
      // supplied only by the Python current-context/authorization service.
      return { ...current.identity, actor: { ...current.identity.actor },
        authentication_time: claims.authenticationTime, identity_checked_at: new Date(now()).toISOString(),
        assurance_level: 'unknown' };
    },
  };
}

export type AuthkitAdapter = ReturnType<typeof createAuthkitAdapter>;
let configured: AuthkitAdapter | undefined;
/** Called once by trusted server composition with durable ports; never a request-controlled override. */
export function configureAuthkitAdapter(adapter: AuthkitAdapter): void {
  if (configured) throw new IdentityFailure('IDENTITY_ALREADY_CONFIGURED', 503);
  configured = adapter;
}
export function configuredHandler(action: 'signin' | 'callback'): (request: Request) => Promise<Response> {
  return async (request) => configured ? configured[action](request)
    : safeError(new IdentityFailure('IDENTITY_NOT_CONFIGURED', 503));
}
