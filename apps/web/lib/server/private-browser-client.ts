import 'server-only';
import { createHash } from 'node:crypto';
import { boundedServerResponse } from './bounded-server-response.js';
import {
  IdentityFailure, type AuthTransaction, type AuthTransactionPort, type Credentials,
  type Identity, type SelectionPort, type ServerSession, type SessionPort,
} from './authkit-adapter.js';

const METHOD_PATHS = {
  create: '/internal/browser-identity/v1/flows/create',
  consume: '/internal/browser-identity/v1/flows/consume',
  admit: '/internal/browser-identity/v1/sessions/admit',
  current: '/internal/browser-identity/v1/sessions/current',
  end: '/internal/browser-identity/v1/sessions/end',
} as const;
type Operation = keyof typeof METHOD_PATHS;
type PrivateFailure = readonly [status: number, code: string];
const FLOW_FIELDS = ['state', 'nonce', 'browserDigest', 'verifier', 'expiresAt', 'returnPath', 'kind',
  'sessionHandle', 'subject', 'sessionId', 'generation', 'selectionTarget', 'selectionIntent'] as const;
const CREDENTIAL_FIELDS = ['accessToken', 'refreshToken', 'userId'] as const;
const BINDING_FIELDS = ['handle', 'csrfToken', 'ended'] as const;
const SIGNING_FIELDS = ['authorization', 'x-amz-date', 'x-amz-security-token'] as const;
const BODY_HEADERS = Object.freeze({ 'content-type': 'application/json' } as const);
const REQUEST_BYTES = 65_536;
const RESPONSE_BYTES = 131_072;
const DEADLINE_MS = 25_000;
const SEMANTIC_FAILURES: Readonly<Record<Operation, readonly PrivateFailure[]>> = {
  create: [[401, 'CALLBACK_INVALID'], [422, 'SCHEMA_INVALID'], [422, 'SESSION_SEAL_INVALID']],
  consume: [[401, 'CALLBACK_INVALID'], [422, 'SCHEMA_INVALID']],
  admit: [[401, 'SESSION_INVALID'], [401, 'SESSION_ENDED'], [409, 'STALE_SESSION_CONTEXT'],
    [422, 'SCHEMA_INVALID'], [422, 'SESSION_SEAL_INVALID']],
  current: [[401, 'SESSION_INVALID'], [401, 'SESSION_ENDED']],
  end: [[401, 'SESSION_INVALID'], [401, 'SESSION_ENDED'], [409, 'STALE_SESSION_CONTEXT'],
    [409, 'SESSION_GENERATION_EXHAUSTED'], [422, 'SCHEMA_INVALID']],
};

export type PrivateBrowserSignInput = Readonly<{
  url: string; method: 'POST'; body: string; headers: typeof BODY_HEADERS;
}>;
export type PrivateBrowserSigningHeaders = Readonly<{
  authorization: string; 'x-amz-date': string; 'x-amz-security-token': string;
  'x-amz-content-sha256'?: string;
}>;
export type PrivateBrowserClientOptions = Readonly<{
  approvedEndpoint: string;
  sign: (request: PrivateBrowserSignInput, signal: AbortSignal) => Promise<PrivateBrowserSigningHeaders>;
  fetch: typeof fetch;
}>;

function closed<K extends string>(value: unknown, keys: readonly K[]): value is Record<K, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}
function text(value: unknown, max: number): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= max;
}
function matches(value: unknown, pattern: RegExp): value is string {
  return typeof value === 'string' && pattern.test(value);
}
function counter(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}
function flowPart(value: unknown): value is string {
  return matches(value, /^[A-Za-z0-9_-]{43}$/);
}
function digest(value: unknown): value is string {
  return matches(value, /^[0-9a-f]{64}$/);
}
function handle(value: unknown): value is string {
  return matches(value, /^[A-Za-z0-9_-]{32,128}$/);
}
function utcTime(value: unknown): value is string {
  if (!matches(value, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/)) return false;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) && new Date(timestamp).toISOString().slice(0, 19) === value.slice(0, 19);
}
function transaction(value: unknown): value is AuthTransaction {
  return closed(value, FLOW_FIELDS) && flowPart(value.state) && flowPart(value.nonce)
    && digest(value.browserDigest) && matches(value.verifier, /^[A-Za-z0-9_-]{43,128}$/)
    && counter(value.expiresAt) && value.expiresAt <= 8_640_000_000_000_000
    && text(value.returnPath, 4096) && value.returnPath.startsWith('/')
    && !value.returnPath.startsWith('//') && !/[\\%]/.test(value.returnPath)
    && ![...value.returnPath].some(character => character.charCodeAt(0) <= 0x20)
    && value.kind === 'signin' && value.sessionHandle === null && value.subject === null
    && value.sessionId === null && value.generation === null && value.selectionTarget === null
    && value.selectionIntent === null;
}
function credentials(value: unknown): value is Credentials {
  return closed(value, CREDENTIAL_FIELDS) && text(value.accessToken, 16_384)
    && text(value.refreshToken, 16_384) && text(value.userId, 256);
}
function identity(value: unknown): value is Identity {
  return closed(value, ['actor', 'session_id', 'session_generation', 'identity_checked_at',
    'authentication_time', 'assurance_level']) && closed(value.actor, ['kind', 'subject', 'issuer'])
    && value.actor.kind === 'human' && text(value.actor.subject, 256) && text(value.actor.issuer, 256)
    && text(value.session_id, 256) && counter(value.session_generation)
    && utcTime(value.identity_checked_at) && utcTime(value.authentication_time)
    && Date.parse(value.authentication_time) <= Date.parse(value.identity_checked_at)
    && value.assurance_level === 'unknown';
}
function session(value: unknown): value is ServerSession {
  if (closed(value, [...BINDING_FIELDS, 'generation'])) {
    return handle(value.handle) && digest(value.csrfToken) && value.ended === true
      && counter(value.generation);
  }
  return closed(value, [...BINDING_FIELDS, 'reconciliationRequired', 'identity', 'credentials'])
    && handle(value.handle) && digest(value.csrfToken) && value.ended === false
    && typeof value.reconciliationRequired === 'boolean' && identity(value.identity)
    && credentials(value.credentials) && value.identity.actor.subject === value.credentials.userId;
}
function errorCode(value: unknown): string | undefined {
  if (closed(value, ['code', 'message', 'request_id', 'retryable', 'field_errors', 'current_revision'])
      && text(value.code, 64) && value.message === 'The identity operation could not be completed.'
      && matches(value.request_id, /^[a-f0-9]{32}$/) && value.retryable === false
      && Array.isArray(value.field_errors) && value.field_errors.length === 0 && value.current_revision === null) {
    return value.code;
  }
  return undefined;
}
function unavailable(operation: Operation): IdentityFailure {
  const code = operation === 'admit' ? 'SESSION_ADMISSION_UNCONFIRMED'
    : operation === 'end' ? 'SESSION_END_UNCONFIRMED' : 'IDENTITY_STATE_UNAVAILABLE';
  return new IdentityFailure(code, 503, false);
}
function invalid(): IdentityFailure {
  return new IdentityFailure('SCHEMA_INVALID', 422);
}
async function disabled(): Promise<never> {
  throw new IdentityFailure('PRIVATE_OPERATION_DISABLED', 403);
}
function endpoint(value: string): string {
  try {
    const url = new URL(value);
    if (url.protocol === 'https:' && (value === url.origin || value === `${url.origin}/dev`)) return value;
  } catch {}
  throw new IdentityFailure('IDENTITY_CONFIGURATION_INVALID', 503, false);
}
function signedHeaders(value: unknown, body: string): Headers {
  if (!closed(value, SIGNING_FIELDS) && !closed(value, [...SIGNING_FIELDS, 'x-amz-content-sha256'])) {
    throw new Error();
  }
  for (const key of ['authorization', 'x-amz-security-token'] as const) {
    if (!text(value[key], 16_384) || !/^[!-~](?:[ -~]*[!-~])?$/.test(value[key])) throw new Error();
  }
  if (!matches(value['x-amz-date'], /^\d{8}T\d{6}Z$/)) throw new Error();
  const headers = new Headers(BODY_HEADERS);
  for (const key of SIGNING_FIELDS) headers.set(key, value[key] as string);
  if ('x-amz-content-sha256' in value) {
    const digest = createHash('sha256').update(body, 'utf8').digest('hex');
    if (value['x-amz-content-sha256'] !== digest) throw new Error();
    headers.set('x-amz-content-sha256', digest);
  }
  return headers;
}

/** Trusted startup supplies the approved endpoint and server-side signing and HTTPS capabilities. */
export function createPrivateBrowserPorts(
  { approvedEndpoint, sign, fetch: transport }: PrivateBrowserClientOptions,
): { transactions: AuthTransactionPort; sessions: SessionPort; selections: SelectionPort } {
  const base = endpoint(approvedEndpoint);
  if (typeof sign !== 'function' || typeof transport !== 'function') {
    throw new IdentityFailure('IDENTITY_CONFIGURATION_INVALID', 503, false);
  }

  async function call(operation: Operation, data: object): Promise<unknown> {
    const request = Object.freeze({
      url: base + METHOD_PATHS[operation], method: 'POST' as const, body: JSON.stringify(data), headers: BODY_HEADERS,
    });
    if (Buffer.byteLength(request.body, 'utf8') > REQUEST_BYTES) throw invalid();
    let response: Response;
    let value: unknown;
    try {
      response = await boundedServerResponse(async (signal, assertTime) => {
        const signed = await sign(request, signal);
        assertTime();
        const result = await transport(request.url, { method: request.method, body: request.body,
          headers: signedHeaders(signed, request.body), signal, redirect: 'error',
          cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer' });
        if (result.redirected || (result.url && result.url !== request.url)) {
          void result.body?.cancel().catch(() => undefined);
          throw unavailable(operation);
        }
        return result;
      }, { timeoutMs: DEADLINE_MS, maxBytes: RESPONSE_BYTES, failure: () => unavailable(operation) });
      if (response.headers.get('content-type') !== 'application/json'
          || response.headers.get('cache-control') !== 'no-store'
          || response.headers.has('content-encoding')) throw unavailable(operation);
      const raw = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(await response.arrayBuffer());
      value = JSON.parse(raw);
    } catch {
      throw unavailable(operation);
    }
    if (response.status === 200) return value;
    const code = errorCode(value);
    if (code && SEMANTIC_FAILURES[operation].some(([status, semantic]) =>
      status === response.status && semantic === code)) throw new IdentityFailure(code, response.status, false);
    throw unavailable(operation);
  }

  return {
    transactions: {
      async create(flow) {
        if (!transaction(flow)) throw invalid();
        if (await call('create', { flow }) !== null) throw unavailable('create');
      },
      async consume(state, browser) {
        if (!matches(state, /^[A-Za-z0-9_-]{43}\.[A-Za-z0-9_-]{43}$/) || !digest(browser)) throw invalid();
        const flow = await call('consume', { state, browser });
        if (!transaction(flow) || `${flow.state}.${flow.nonce}` !== state || flow.browserDigest !== browser) {
          throw unavailable('consume');
        }
        return flow;
      },
    },
    sessions: {
      async admit(candidate, claims) {
        const { issuer, subject, sessionId } = claims;
        if (!credentials(candidate) || candidate.userId !== subject) throw invalid();
        const submitted = { accessToken: candidate.accessToken, refreshToken: candidate.refreshToken,
          userId: candidate.userId };
        const admitted = await call('admit', { credentials: submitted });
        if (!session(admitted) || admitted.ended || admitted.identity.actor.issuer !== issuer
            || admitted.identity.actor.subject !== subject || admitted.identity.session_id !== sessionId
            || CREDENTIAL_FIELDS.some(key => admitted.credentials[key] !== submitted[key])) {
          throw unavailable('admit');
        }
        return admitted;
      },
      async current(binding) {
        if (!handle(binding)) throw invalid();
        const current = await call('current', { handle: binding });
        if (!session(current) || current.handle !== binding) throw unavailable('current');
        return current;
      },
      async end(binding, generation) {
        if (!handle(binding) || !counter(generation)) throw invalid();
        const receipt = await call('end', { handle: binding, generation });
        if (!closed(receipt, ['sessionId', 'generation']) || !text(receipt.sessionId, 256)
            || !counter(receipt.generation)) throw unavailable('end');
        return { sessionId: receipt.sessionId, generation: receipt.generation };
      },
      replaceCredentials: disabled,
      requireReconciliation: disabled,
    },
    selections: { authorize: disabled, commit: disabled },
  };
}
