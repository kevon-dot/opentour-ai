import 'server-only';
import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { WorkOS } from '@workos-inc/node';
import { createLocalJWKSet, decodeProtectedHeader, jwtVerify, type JWTVerifyGetKey,
  type JSONWebKeySet } from 'jose';

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
export type SelectionTarget = Readonly<{
  organizationId: string; identityOrganizationId: string; identityIssuer: string;
  membershipId: string; authorityRevision: string;
}>;
export type SelectionIntent = Readonly<{
  organizationId: string; expectedGeneration: number; idempotencyKey: string;
}>;
export type SelectionReceipt = Readonly<{
  state: 'active'; organization_id: string; membership_id: string;
  session_generation: number; request_id: string;
}>;
export type SelectionDecision = { target: SelectionTarget; alreadyCurrent?: boolean } | { receipt: SelectionReceipt };
export type ServerSession = {
  handle: string; identity: Identity; credentials: Credentials; csrfToken: string;
  ended: boolean;
  reconciliationRequired: boolean;
  credentialDigest?: string;
};
export type AuthTransaction = {
  state: string; nonce: string; browserDigest: string; verifier: string; expiresAt: number;
  returnPath: string; kind: 'signin' | 'switch';
  sessionHandle: string | null; subject: string | null; sessionId: string | null;
  generation: number | null; selectionTarget: SelectionTarget | null;
  selectionIntent: SelectionIntent | null;
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
  /** Return the digest of the exact sealed payload committed under the generation fence. */
  replaceCredentials(handle: string, generation: number, credentials: Credentials,
    claims: Claims): Promise<string>;
  /** Durable idempotent ended marker + generation increment; uncertain commit must throw. */
  end(handle: string, generation: number): Promise<{ sessionId: string; generation: number }>;
  /** Mark private-context admission unavailable without ending the valid human identity. */
  requireReconciliation(handle: string, generation: number): Promise<void>;
}
export interface SelectionPort {
  /** Current171/172 local membership and committed provider mapping; never infer equal IDs. */
  authorize(handle: string, identity: Identity, intent: SelectionIntent): Promise<SelectionDecision>;
  /**172 rechecks current mapping, generation, ended state and the staged ciphertext digest.
   * Persist the local selection and idempotent receipt in one transaction. */
  commit(handle: string, identity: Identity, intent: SelectionIntent, target: SelectionTarget,
    claims: Claims, credentialDigest: string): Promise<SelectionReceipt>;
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
const targetFields = ['organizationId', 'identityOrganizationId', 'identityIssuer',
  'membershipId', 'authorityRevision'] as const;
function checkedTarget(identity: Identity, localOrganizationId: string,
  value: SelectionTarget): SelectionTarget {
  if (!value || Object.keys(value).length !== targetFields.length
      || !targetFields.every((field) => validId(value[field]))
      || value.organizationId !== localOrganizationId
      || value.identityIssuer !== identity.actor.issuer) {
    throw new IdentityFailure('IDENTITY_MEMBERSHIP_CONFLICT', 403);
  }
  // Snapshot the trusted port result before any await; mutable references cannot change intent.
  return Object.freeze({ organizationId: value.organizationId,
    identityOrganizationId: value.identityOrganizationId, identityIssuer: value.identityIssuer,
    membershipId: value.membershipId, authorityRevision: value.authorityRevision });
}
function checkedIntent(organizationId: string, expectedGeneration: number,
  idempotencyKey: string): SelectionIntent {
  if (!validId(organizationId) || !validGeneration(expectedGeneration)
      || !validId(idempotencyKey) || /[^!-~]/.test(idempotencyKey)) {
    throw new IdentityFailure('SCHEMA_INVALID', 422);
  }
  return Object.freeze({ organizationId, expectedGeneration, idempotencyKey });
}
function checkedDigest(value: unknown): string {
  if (typeof value !== 'string' || value.length !== 64 || /[^a-f0-9]/.test(value)) {
    throw new IdentityFailure('SESSION_CREDENTIALS_UNAVAILABLE', 503);
  }
  return value;
}
function checkedReceipt(receipt: SelectionReceipt, intent: SelectionIntent,
  target?: SelectionTarget): SelectionReceipt {
  const { state, organization_id, membership_id, session_generation, request_id } = receipt;
  if (state !== 'active' || organization_id !== intent.organizationId || !validId(membership_id)
      || (target && membership_id !== target.membershipId) || !validGeneration(session_generation)
      || session_generation < intent.expectedGeneration || session_generation - intent.expectedGeneration > 1
      || !validId(request_id)) {
    throw new IdentityFailure('SELECTION_RECEIPT_UNCONFIRMED', 503);
  }
  return { state, organization_id, membership_id, session_generation, request_id };
}
function sameTarget(expected: SelectionTarget, current: SelectionTarget): void {
  if (!targetFields.every((field) => expected[field] === current[field])) {
    throw new IdentityFailure('STALE_SESSION_CONTEXT', 409);
  }
}
function serverClock(clock: () => number): () => number {
  return () => {
    const value = clock();
    if (!Number.isSafeInteger(value) || value < 0 || value > 8_640_000_000_000_000) {
      throw new IdentityFailure('IDENTITY_CLOCK_UNAVAILABLE', 503);
    }
    return value;
  };
}

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
  if (session.ended || !validGeneration(session.identity.session_generation)
      || session.identity.actor.kind !== 'human' || !/^[A-Za-z0-9_-]{32,128}$/.test(session.handle)) {
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

function projectedIdentity(claims: Claims, session: ServerSession, checkedAt: number): Identity {
  return {
    actor: { kind: 'human', subject: claims.subject, issuer: claims.issuer },
    session_id: claims.sessionId,
    session_generation: session.identity.session_generation,
    identity_checked_at: new Date(checkedAt).toISOString(),
    authentication_time: claims.authenticationTime,
    assurance_level: 'unknown',
  };
}

/** Bound the entire response stream, not just receipt of HTTP headers. */
async function boundedResponse(fetcher: typeof fetch, input: Parameters<typeof fetch>[0],
  init: Parameters<typeof fetch>[1] = {}): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5_000);
  const signal = init.signal ? AbortSignal.any([init.signal, controller.signal]) : controller.signal;
  let rejectAbort: () => void = () => undefined;
  const aborted = new Promise<never>((_resolve, reject) => {
    rejectAbort = () => reject(new IdentityFailure('PROVIDER_RESPONSE_UNAVAILABLE', 503));
    signal.addEventListener('abort', rejectAbort, { once: true });
  });
  const wait = <T>(work: Promise<T>): Promise<T> => Promise.race([work, aborted]);
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  try {
    if (signal.aborted) throw new IdentityFailure('PROVIDER_RESPONSE_UNAVAILABLE', 503);
    const response = await wait(fetcher(input, { ...init, signal, redirect: 'error' }));
    if (!response.body) return response;
    reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    while (true) {
      const { value, done } = await wait(reader.read());
      if (done) break;
      size += value.byteLength;
      if (size > 65_536 || signal.aborted) throw new IdentityFailure('PROVIDER_RESPONSE_UNAVAILABLE', 503);
      chunks.push(value);
    }
    return new Response(Buffer.concat(chunks), { status: response.status,
      statusText: response.statusText, headers: response.headers });
  } finally {
    clearTimeout(timer);
    signal.removeEventListener('abort', rejectAbort);
    controller.abort();
    if (reader) void reader.cancel().catch(() => undefined);
  }
}

function trustedKeys(profile: AuthProfile, fetcher: typeof fetch): JWTVerifyGetKey {
  let snapshot: { get: JWTVerifyGetKey; ids: ReadonlySet<string>; loaded: number } | undefined;
  let pending: Promise<void> | undefined;
  let attempted = -Infinity;
  return async (header, token) => {
    const time = performance.now();
    const fresh = snapshot && time >= snapshot.loaded && time - snapshot.loaded < 300_000;
    if (fresh && snapshot!.ids.has(header.kid ?? '')) return snapshot!.get(header, token);
    if (pending) throw new IdentityFailure('IDENTITY_VERIFICATION_UNAVAILABLE', 503);
    if (time - attempted < 30_000) {
      throw new IdentityFailure(fresh ? 'SESSION_INVALID' : 'IDENTITY_VERIFICATION_UNAVAILABLE', fresh ? 401 : 503);
    }
    attempted = time;
    pending = (async () => {
      const response = await boundedResponse(fetcher, profile.jwksURL);
      if (response.status !== 200) throw new Error();
      const document = await response.json() as JSONWebKeySet;
      if (!Array.isArray(document.keys) || document.keys.length < 1 || document.keys.length > 32) throw new Error();
      const ids = new Set<string>();
      for (const key of document.keys) {
        if (!key || key.kty !== 'RSA' || !validId(key.kid) || ids.has(key.kid)
            || (key.alg !== undefined && key.alg !== 'RS256') || (key.use !== undefined && key.use !== 'sig')
            || (key.key_ops !== undefined && JSON.stringify(key.key_ops) !== '["verify"]')) throw new Error();
        ids.add(key.kid);
      }
      if (performance.now() - time >= 5_000) throw new Error();
      snapshot = { get: createLocalJWKSet(document), ids, loaded: performance.now() };
    })();
    try { await pending; }
    catch { throw new IdentityFailure('IDENTITY_VERIFICATION_UNAVAILABLE', 503); }
    finally { pending = undefined; }
    if (!snapshot || !snapshot.ids.has(header.kid ?? '')) throw new IdentityFailure('SESSION_INVALID');
    return snapshot.get(header, token);
  };
}

export function createWorkOSProvider(profile: AuthProfile, apiKey: string,
  options: { fetch?: typeof fetch; key?: JWTVerifyGetKey; now?: () => number } = {}): ProviderPort {
  profileCheck(profile);
  const now = serverClock(options.now ?? Date.now);
  const fetcher = options.fetch ?? globalThis.fetch;
  const sdk = new WorkOS(apiKey, { clientId: profile.clientId, issuer: profile.issuer,
    timeout: 5_000, maxRetries: 0, fetchFn: (input, init) => boundedResponse(fetcher, input, init) });
  const jwks = options.key ?? trustedKeys(profile, fetcher);
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
        const started = now();
        if (typeof token !== 'string' || token.length > 16_384) throw new Error();
        const header = decodeProtectedHeader(token);
        if (!validId(header.kid) || header.alg !== 'RS256' || header.jku || header.jwk || header.x5u) throw new Error();
        const { payload } = await jwtVerify(token, jwks, { issuer: profile.issuer,
          audience: profile.audience, algorithms: ['RS256'], clockTolerance: 0,
          currentDate: new Date(started), requiredClaims: ['iss', 'aud', 'sub', 'sid', 'iat', 'exp', 'auth_time'] });
        if (now() < started) throw new IdentityFailure('IDENTITY_CLOCK_UNAVAILABLE', 503);
        if (!validId(payload.sub) || !validId(payload.sid) || payload.aud !== profile.audience
            || !Number.isSafeInteger(payload.auth_time) || !Number.isSafeInteger(payload.iat)
            || !Number.isSafeInteger(payload.exp) || (payload.auth_time as number) < 0
            || (payload.auth_time as number) > payload.iat! || payload.iat! > now() / 1000
            || now() >= payload.exp! * 1000
            || payload.exp! <= payload.iat! || (payload.sub_profile !== undefined && payload.sub_profile !== 'user')
            || payload.sub_type !== undefined || (payload.org_id !== undefined && !validId(payload.org_id))) throw new Error();
        return { issuer: profile.issuer, subject: payload.sub, sessionId: payload.sid,
          organizationId: typeof payload.org_id === 'string' ? payload.org_id : null,
          authenticationTime: new Date((payload.auth_time as number) * 1000).toISOString(),
          expiresAt: payload.exp! * 1000 };
      } catch (error) {
        if (error instanceof IdentityFailure) throw error;
        if ((error as { code?: string })?.code === 'ERR_JWKS_TIMEOUT') {
          throw new IdentityFailure('IDENTITY_VERIFICATION_UNAVAILABLE', 503);
        }
        throw new IdentityFailure('SESSION_INVALID');
      }
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
  const now = serverClock(ports.now ?? Date.now);

  async function rebound(session: ServerSession): Promise<ServerSession> {
    const current = await sessions.current(session.handle);
    active(current, session.identity.session_generation);
    if (current.handle !== session.handle || current.identity.session_id !== session.identity.session_id
        || current.identity.actor.subject !== session.identity.actor.subject
        || current.identity.actor.issuer !== session.identity.actor.issuer) {
      throw new IdentityFailure('SESSION_INVALID');
    }
    return current;
  }

  async function verifiedSession(session: ServerSession): Promise<{ session: ServerSession; claims: Claims }> {
    const claims = await provider.verify(session.credentials.accessToken);
    sameSession(session, claims);
    const current = await rebound(session);
    if (current.credentials.accessToken !== session.credentials.accessToken) {
      throw new IdentityFailure('STALE_SESSION_CONTEXT', 409);
    }
    const checkedAt = now();
    if (claims.expiresAt <= checkedAt) throw new IdentityFailure('SESSION_INVALID');
    return { session: { ...current, identity: projectedIdentity(claims, current, checkedAt) }, claims };
  }

  async function authorizedTarget(session: ServerSession, intent: SelectionIntent): Promise<SelectionTarget> {
    const decision = await selections.authorize(session.handle, session.identity, intent);
    if ('receipt' in decision) throw new IdentityFailure('STALE_SESSION_CONTEXT', 409);
    return checkedTarget(session.identity, intent.organizationId, decision.target);
  }

  async function commitSwitch(session: ServerSession, intent: SelectionIntent, target: SelectionTarget,
    credentials: Credentials, claims: Claims): Promise<SelectionReceipt> {
    sameSession(session, claims, target.identityOrganizationId);
    if (credentials.userId !== claims.subject || claims.expiresAt <= now()) {
      throw new IdentityFailure('SESSION_INVALID');
    }
    const generation = intent.expectedGeneration;
    await rebound(session);
    const digest = checkedDigest(await sessions.replaceCredentials(session.handle, generation, credentials, claims));
    sameTarget(target, await authorizedTarget(session, intent));
    if (claims.expiresAt <= now()) throw new IdentityFailure('SESSION_INVALID');
    return checkedReceipt(await selections.commit(
      session.handle, session.identity, intent, target, claims, digest,
    ), intent, target);
  }

  async function start(returnPath: string, browser: string, session?: ServerSession,
    selectionTarget?: SelectionTarget, selectionIntent?: SelectionIntent): Promise<Response> {
    checkedReturn(profile, returnPath);
    const transaction: AuthTransaction = { state: secret(), nonce: secret(), browserDigest: sha(browser),
      verifier: secret(), expiresAt: now() + 600_000, returnPath,
      kind: session ? 'switch' : 'signin', sessionHandle: session?.handle ?? null,
      subject: session?.identity.actor.subject ?? null, sessionId: session?.identity.session_id ?? null,
      generation: session?.identity.session_generation ?? null, selectionTarget: selectionTarget ?? null,
      selectionIntent: selectionIntent ?? null };
    const location = providerDestination(profile, await provider.authorizationURL(
      `${transaction.state}.${transaction.nonce}`, transaction.verifier, selectionTarget?.identityOrganizationId));
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
        let target: SelectionTarget | undefined;
        let intent: SelectionIntent | undefined;
        if (transaction.kind === 'switch') {
          if (!transaction.sessionHandle || transaction.generation === null
              || !transaction.selectionTarget || !transaction.selectionIntent) {
            throw new IdentityFailure('CALLBACK_INVALID');
          }
          intent = checkedIntent(transaction.selectionIntent.organizationId,
            transaction.selectionIntent.expectedGeneration, transaction.selectionIntent.idempotencyKey);
          if (intent.expectedGeneration !== transaction.generation) throw new IdentityFailure('CALLBACK_INVALID');
          previous = await sessions.current(transaction.sessionHandle);
          active(previous, transaction.generation);
          if (previous.identity.actor.subject !== transaction.subject
              || previous.identity.session_id !== transaction.sessionId) throw new IdentityFailure('CALLBACK_INVALID');
          target = checkedTarget(previous.identity, intent.organizationId, transaction.selectionTarget);
          sameTarget(target, await authorizedTarget(previous, intent));
          await rebound(previous);
          if (transaction.expiresAt <= now()) throw new IdentityFailure('CALLBACK_INVALID');
        }
        // Durable claim is already consumed. Lost exchange outcome cannot automatically retry the code.
        const credentials = await provider.exchange(codeValues[0], transaction.verifier);
        const claims = await provider.verify(credentials.accessToken);
        if (transaction.expiresAt <= now() || claims.expiresAt <= now()) throw new IdentityFailure('CALLBACK_INVALID');
        if (claims.subject !== credentials.userId) throw new IdentityFailure('SESSION_INVALID');
        let session: ServerSession;
        if (previous && target && intent) {
          const receipt = await commitSwitch(previous, intent, target, credentials, claims);
          session = await sessions.current(previous.handle);
          active(session, receipt.session_generation);
        } else {
          session = await sessions.admit(credentials, claims);
        }
        active(session);
        sameSession(session, claims);
        if (claims.expiresAt <= now()) throw new IdentityFailure('SESSION_INVALID');
        const headers = noStore();
        headers.append('Set-Cookie', setCookie(flowCookie, '', 0));
        headers.append('Set-Cookie', setCookie(sessionCookie, session.handle));
        return redirect(new URL(transaction.returnPath, profile.origin).href, headers);
      } catch (error) {
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
      { outcome: 'verified_target' | 'replayed'; receipt: SelectionReceipt } |
      { outcome: 'denied' | 'transient' | 'unknown' } |
      { outcome: 'authentication_required'; response: Response }> {
      const intent = checkedIntent(targetOrganization, observedGeneration, request.headers.get('Idempotency-Key') ?? '');
      const bound = await sessions.current(cookie(request, sessionCookie));
      mutation(request, profile, bound);
      active(bound);
      checkedReturn(profile, returnPath);
      const { session, claims: identityClaims } = await verifiedSession(bound);
      const decision = await selections.authorize(session.handle, session.identity, intent);
      const current = await rebound(session);
      if (current.credentials.accessToken !== session.credentials.accessToken) {
        throw new IdentityFailure('STALE_SESSION_CONTEXT', 409);
      }
      if (identityClaims.expiresAt <= now()) throw new IdentityFailure('SESSION_INVALID');
      if ('receipt' in decision) return { outcome: 'replayed', receipt: checkedReceipt(decision.receipt, intent) };
      active(current, observedGeneration);
      const target = checkedTarget(session.identity, intent.organizationId, decision.target);
      if (decision.alreadyCurrent) {
        if (current.reconciliationRequired || identityClaims.organizationId !== target.identityOrganizationId) {
          throw new IdentityFailure('CONTEXT_RECONCILIATION_REQUIRED', 409);
        }
        const digest = checkedDigest(current.credentialDigest);
        const receipt = checkedReceipt(await selections.commit(
          current.handle, session.identity, intent, target, identityClaims, digest,
        ), intent, target);
        await rebound(current);
        if (identityClaims.expiresAt <= now()) throw new IdentityFailure('SESSION_INVALID');
        return { outcome: 'verified_target', receipt };
      }
      // Persist fail-closed admission before an external mutation can have an unknown
      // outcome. A later marker-write failure must not leave the old tenant usable.
      await sessions.requireReconciliation(session.handle, observedGeneration);
      let credentials: Credentials;
      try { credentials = await provider.refresh(current.credentials, target.identityOrganizationId); }
      catch (error) {
        if (error instanceof ProviderFailure) {
          if (error.outcome === 'authentication_required') {
            sameTarget(target, await authorizedTarget(session, intent));
            await rebound(session);
            return { outcome: error.outcome, response: await start(returnPath, secret(), session, target, intent) };
          }
          return { outcome: error.outcome };
        }
        return { outcome: 'unknown' };
      }
      try {
        const claims = await provider.verify(credentials.accessToken);
        const receipt = await commitSwitch(session, intent, target, credentials, claims);
        if (claims.expiresAt <= now()) throw new IdentityFailure('SESSION_INVALID');
        return { outcome: 'verified_target', receipt };
      } catch {
        // The marker was committed before refresh; an older attempt cannot overwrite a newer result.
        return { outcome: 'unknown' };
      }
    },
    async verifiedIdentity(request: Request, use: IdentityUse): Promise<Identity> {
      if (!identityUses.includes(use)) throw new IdentityFailure('CAPABILITY_DENIED', 403);
      const session = await sessions.current(cookie(request, sessionCookie));
      active(session);
      const verified = await verifiedSession(session);
      return verified.session.identity;
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
