import 'server-only';
import { createHash, createHmac, type Hash, type Hmac } from 'node:crypto';
import { SignatureV4 } from '@smithy/signature-v4';
import { IdentityFailure } from './authkit-adapter.js';
import {
  PRIVATE_BROWSER_METHOD_PATHS, PRIVATE_BROWSER_REQUEST_BYTES, type PrivateBrowserClientOptions,
} from './private-browser-client.js';

type TemporaryCredentials = Readonly<{
  accessKeyId: string; secretAccessKey: string; sessionToken: string; expiration: Date;
}>;
export type PrivateGatewaySignerOptions = Readonly<{
  approvedEndpoint: string;
  region: string;
  restApiId: string;
  vpcEndpointId: string;
  credentials: (signal: AbortSignal) => Promise<TemporaryCredentials>;
  now?: () => number;
}>;

type SourceData = string | ArrayBuffer | ArrayBufferView;
function bytes(value: SourceData, encoding?: string): Buffer {
  if (typeof value === 'string') return Buffer.from(value, encoding as BufferEncoding | undefined);
  if (ArrayBuffer.isView(value)) return Buffer.from(value.buffer, value.byteOffset, value.byteLength);
  return Buffer.from(value);
}

/** Node's hash primitives implement the SDK contract. Smithy owns SigV4. */
class NodeSha256 {
  private readonly state: Hash | Hmac;

  constructor(secret?: SourceData) {
    this.state = secret === undefined ? createHash('sha256') : createHmac('sha256', bytes(secret));
  }

  update(value: SourceData, encoding?: string): void {
    this.state.update(bytes(value, encoding));
  }

  async digest(): Promise<Uint8Array> {
    return this.state.digest();
  }
}

function liveTime(value: number): boolean {
  return Number.isFinite(value) && value >= 0 && value <= 8_640_000_000_000_000;
}
function credentialField(value: unknown, max: number): value is string {
  return typeof value === 'string' && value.length <= max && /^[!-~]+$/.test(value);
}
function refusal(): IdentityFailure {
  return new IdentityFailure('PRIVATE_SIGNING_UNAVAILABLE', 503, false);
}

/** Bind an explicit temporary credential provider to one approved private dev endpoint. */
export function createPrivateGatewaySigner({
  approvedEndpoint, region, restApiId, vpcEndpointId, credentials, now = Date.now,
}: PrivateGatewaySignerOptions): PrivateBrowserClientOptions['sign'] {
  if (typeof region !== 'string' || !/^[a-z]{2}(?:-[a-z]+)+-\d+$/.test(region)
      || typeof restApiId !== 'string' || !/^[a-z0-9]{1,64}$/.test(restApiId)
      || typeof vpcEndpointId !== 'string' || !/^vpce-(?:[a-f0-9]{8}|[a-f0-9]{17})$/.test(vpcEndpointId)
      || typeof credentials !== 'function' || typeof now !== 'function') {
    throw new IdentityFailure('IDENTITY_CONFIGURATION_INVALID', 503, false);
  }
  const host = `${restApiId}-${vpcEndpointId}.execute-api.${region}.amazonaws.com`;
  if (approvedEndpoint !== `https://${host}/dev`) {
    throw new IdentityFailure('IDENTITY_CONFIGURATION_INVALID', 503, false);
  }
  const urls = new Set(Object.values(PRIVATE_BROWSER_METHOD_PATHS).map(path => approvedEndpoint + path));

  return async (request, signal) => {
    try {
      const { url, method, body, headers } = request;
      if (!urls.has(url) || method !== 'POST' || typeof body !== 'string'
          || Buffer.byteLength(body, 'utf8') > PRIVATE_BROWSER_REQUEST_BYTES
          || Object.keys(headers).length !== 1 || !Object.hasOwn(headers, 'content-type')
          || headers['content-type'] !== 'application/json' || signal.aborted) throw refusal();
      const started = now();
      if (!liveTime(started)) throw refusal();
      const offered = await credentials(signal);
      const { accessKeyId, secretAccessKey, sessionToken, expiration } = offered;
      const expires = expiration instanceof Date ? expiration.getTime() : NaN;
      const signingTime = now();
      if (signal.aborted || !liveTime(signingTime) || signingTime < started
          || !liveTime(expires) || expires <= signingTime
          || !credentialField(accessKeyId, 256) || !/^[A-Za-z0-9_-]+$/.test(accessKeyId)
          || !credentialField(secretAccessKey, 1024)
          || !credentialField(sessionToken, 16_384)) throw refusal();

      const sdk = new SignatureV4({ service: 'execute-api', region, sha256: NodeSha256,
        applyChecksum: true, credentials: { accessKeyId, secretAccessKey, sessionToken } });
      const result = await sdk.sign({
        protocol: 'https:', hostname: host, method, path: new URL(url).pathname,
        headers: { host, 'content-type': 'application/json' }, body,
      }, { signingDate: new Date(signingTime) });
      const checkedAt = now();
      const signed = result.headers;
      if (signal.aborted || !liveTime(checkedAt) || checkedAt < signingTime || expires <= checkedAt
          || result.body !== body || signed['x-amz-security-token'] !== sessionToken
          || !signed.authorization || !signed['x-amz-date'] || !signed['x-amz-content-sha256']) throw refusal();
      return Object.freeze({
        authorization: signed.authorization,
        'x-amz-date': signed['x-amz-date'],
        'x-amz-security-token': signed['x-amz-security-token'],
        'x-amz-content-sha256': signed['x-amz-content-sha256'],
      });
    } catch {
      throw refusal();
    }
  };
}
