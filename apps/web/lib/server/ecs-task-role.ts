import 'server-only';

import http from 'node:http';
import { Readable } from 'node:stream';
import { IdentityFailure } from './authkit-adapter.js';
import { boundedServerResponse } from './bounded-server-response.js';
import type { PrivateGatewaySignerOptions } from './private-gateway-signer.js';

const CREDENTIALS_ORIGIN = 'http://169.254.170.2';
const RELATIVE_URI = /^\/v2\/credentials\/[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}$/;
const ROLE_ARN = /^arn:aws(?:-[a-z0-9]+)*:iam::[0-9]{12}:role\/(?:[!-~]+\/)?[A-Za-z0-9+=,.@_-]{1,64}$/;
const UTC_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$/;
const RESPONSE_BYTES = 24_576;
const DEADLINE_MS = 2_000;
const RESPONSE_FIELDS = ['RoleArn', 'AccessKeyId', 'SecretAccessKey', 'Token', 'Expiration'] as const;
// An explicit agent without proxy settings bypasses Node's configurable global proxy.
const directAgent = new http.Agent({ keepAlive: false });

type EcsTaskCredentialOptions = Readonly<{
  relativeUri: string | undefined;
  expectedRoleArn: string;
  fetch: typeof fetch;
  now?: () => number;
}>;

function unavailable(): IdentityFailure {
  return new IdentityFailure('ECS_TASK_CREDENTIALS_UNAVAILABLE', 503, false);
}

function credentialsPath(value: unknown): value is string {
  return typeof value === 'string' && value.length === 52 && RELATIVE_URI.test(value);
}

function credentialField(value: unknown, max: number): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= max && !/[^!-~]/.test(value);
}

function credentialsResponse(value: unknown): value is Record<typeof RESPONSE_FIELDS[number], unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === RESPONSE_FIELDS.length
    && RESPONSE_FIELDS.every(field => Object.hasOwn(value, field));
}

/** Direct HTTP transport for the bounded ECS task-role credential provider. */
export const fetchEcsTaskCredentials: typeof fetch = async (input, init) => {
  if (typeof input !== 'string' || !input.startsWith(CREDENTIALS_ORIGIN)
      || !credentialsPath(input.slice(CREDENTIALS_ORIGIN.length))
      || init?.method !== 'GET' || init.body != null || init.redirect !== 'error'
      || !init.signal || init.signal.aborted) throw unavailable();

  return new Promise<Response>((resolve, reject) => {
    const request = http.request(input, {
      agent: directAgent, method: 'GET', headers: { Accept: 'application/json' }, signal: init.signal ?? undefined,
    }, incoming => {
      try {
        if (incoming.statusCode !== 200) throw unavailable();
        const headers = new Headers();
        for (const name of ['content-type', 'content-encoding']) {
          const value = incoming.headers[name];
          if (typeof value === 'string') headers.set(name, value);
        }
        const stream = Readable.toWeb(incoming, { strategy: { highWaterMark: 1 } });
        resolve(new Response(stream as ReadableStream<Uint8Array>, { status: 200, headers }));
      } catch {
        incoming.destroy();
        reject(unavailable());
      }
    });
    request.once('error', () => reject(unavailable()));
    request.end();
  });
};

/** Resolve one temporary credential snapshot from the approved ECS task role. */
export function createEcsTaskCredentialProvider({
  relativeUri, expectedRoleArn, fetch: transport, now = Date.now,
}: EcsTaskCredentialOptions): PrivateGatewaySignerOptions['credentials'] {
  if (!credentialsPath(relativeUri)
      || !credentialField(expectedRoleArn, 768) || !ROLE_ARN.test(expectedRoleArn)
      || typeof transport !== 'function' || typeof now !== 'function') {
    throw new IdentityFailure('IDENTITY_CONFIGURATION_INVALID', 503, false);
  }
  const endpoint = CREDENTIALS_ORIGIN + relativeUri;

  return async signal => {
    try {
      const response = await boundedServerResponse(async requestSignal => {
        const result = await transport(endpoint, {
          method: 'GET', headers: { Accept: 'application/json' }, signal: requestSignal,
          redirect: 'error', cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer',
        });
        const mediaType = result.headers.get('content-type')?.split(';', 1)[0]?.trim().toLowerCase();
        if (result.status !== 200 || result.redirected || (result.url && result.url !== endpoint)
            || mediaType !== 'application/json' || result.headers.has('content-encoding')) {
          void result.body?.cancel().catch(() => undefined);
          throw unavailable();
        }
        return result;
      }, { signal, timeoutMs: DEADLINE_MS, maxBytes: RESPONSE_BYTES, failure: unavailable });
      const raw = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(await response.arrayBuffer());
      const data: unknown = JSON.parse(raw);
      if (!credentialsResponse(data) || data.RoleArn !== expectedRoleArn
          || !credentialField(data.AccessKeyId, 256) || !/^[A-Za-z0-9_-]+$/.test(data.AccessKeyId)
          || !credentialField(data.SecretAccessKey, 1024) || !credentialField(data.Token, 16_384)
          || !credentialField(data.Expiration, 30) || !UTC_TIMESTAMP.test(data.Expiration)) {
        throw unavailable();
      }
      const expiration = new Date(data.Expiration);
      const checkedAt = now();
      if (signal.aborted || !Number.isSafeInteger(checkedAt) || checkedAt < 0
          || !Number.isFinite(expiration.getTime()) || expiration.getTime() <= checkedAt
          || expiration.toISOString().slice(0, 19) !== data.Expiration.slice(0, 19)) {
        throw unavailable();
      }
      return {
        accessKeyId: data.AccessKeyId, secretAccessKey: data.SecretAccessKey,
        sessionToken: data.Token, expiration,
      };
    } catch {
      throw unavailable();
    }
  };
}
