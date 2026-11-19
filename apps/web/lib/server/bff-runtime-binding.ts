import 'server-only';

import { createHash } from 'node:crypto';
import { constants } from 'node:fs';
import { open, realpath } from 'node:fs/promises';
import { isAbsolute, resolve } from 'node:path';
import { IdentityFailure, type AuthProfile } from './authkit-adapter.js';
import type { PrivateGatewaySignerOptions } from './private-gateway-signer.js';

type GatewayBinding = Readonly<Pick<PrivateGatewaySignerOptions,
  'approvedEndpoint' | 'region' | 'restApiId' | 'vpcEndpointId'> & { taskRoleArn: string }>;

export type BffRuntimeBinding = Readonly<{
  profile: Readonly<AuthProfile>;
  gateway: GatewayBinding;
}>;

const MAX_BINDING_BYTES = 16_384;
const PROFILE_FIELDS = [
  'origin', 'callbackURI', 'signedOutPath', 'returnPaths', 'providerOrigins',
  'issuer', 'audience', 'jwksURL', 'clientId',
] as const;
const GATEWAY_FIELDS = ['approvedEndpoint', 'region', 'restApiId', 'vpcEndpointId', 'taskRoleArn'] as const;
const TASK_ROLE_ARN = /^arn:aws:iam::[0-9]{12}:role\/(?:[!-~]+\/)?[A-Za-z0-9_+=,.@-]{1,64}$/;

function invalid(): IdentityFailure {
  return new IdentityFailure('IDENTITY_CONFIGURATION_INVALID', 503, false);
}

function closed<K extends string>(value: unknown, names: readonly K[]): value is Record<K, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === names.length && names.every(name => Object.hasOwn(value, name));
}

function text(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.trim() === value;
}

function textList(value: unknown): value is string[] {
  return Array.isArray(value) && value.length > 0 && value.every(text);
}

function readProfile(value: unknown): Readonly<AuthProfile> {
  if (!closed(value, PROFILE_FIELDS)) throw invalid();
  const { origin, callbackURI, signedOutPath, returnPaths, providerOrigins,
    issuer, audience, jwksURL, clientId } = value;
  if (!text(origin) || !text(callbackURI) || !text(signedOutPath) || !textList(returnPaths)
      || !textList(providerOrigins) || !text(issuer) || !text(audience) || !text(jwksURL)
      || !text(clientId) || callbackURI !== origin + '/auth/callback') throw invalid();

  // The AuthKit startup validates the profile's origin and return-destination policy.
  return Object.freeze({
    origin, callbackURI, signedOutPath,
    returnPaths: Object.freeze(returnPaths), providerOrigins: Object.freeze(providerOrigins),
    issuer, audience, jwksURL, clientId,
  });
}

function readGateway(value: unknown): GatewayBinding {
  if (!closed(value, GATEWAY_FIELDS)) throw invalid();
  const { approvedEndpoint, region, restApiId, vpcEndpointId, taskRoleArn } = value;
  if (!text(approvedEndpoint) || !text(region) || !/^[a-z]{2}(?:-[a-z]+)+-\d+$/.test(region)
      || !text(restApiId) || !/^[a-z0-9]{1,64}$/.test(restApiId)
      || !text(vpcEndpointId) || !/^vpce-(?:[a-f0-9]{8}|[a-f0-9]{17})$/.test(vpcEndpointId)
      || !text(taskRoleArn) || !TASK_ROLE_ARN.test(taskRoleArn)) throw invalid();
  const host = `${restApiId}-${vpcEndpointId}.execute-api.${region}.amazonaws.com`;
  if (approvedEndpoint !== `https://${host}/dev`) throw invalid();
  return Object.freeze({ approvedEndpoint, region, restApiId, vpcEndpointId, taskRoleArn });
}

async function readApprovedFile(file: string): Promise<Buffer> {
  if (await realpath(file) !== file) throw invalid();
  const handle = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.size < 1 || info.size > MAX_BINDING_BYTES
        || await realpath(file) !== file) throw invalid();

    // Read at most one byte beyond the inspected size so a changing file cannot exceed the bound.
    const bytes = Buffer.alloc(info.size + 1);
    let offset = 0;
    while (offset < bytes.length) {
      const { bytesRead } = await handle.read(bytes, offset, bytes.length - offset, offset);
      if (bytesRead === 0) break;
      offset += bytesRead;
    }
    if (offset !== info.size) throw invalid();
    return bytes.subarray(0, offset);
  } finally {
    await handle.close();
  }
}

/** Load the read-only binding using an independently supplied digest of the exact file bytes. */
export async function readBffRuntimeBinding(
  file: string | undefined, expectedSha256: string | undefined,
): Promise<BffRuntimeBinding> {
  try {
    if (!file || !isAbsolute(file) || resolve(file) !== file
        || !expectedSha256 || !/^[a-f0-9]{64}$/.test(expectedSha256)) throw invalid();
    const bytes = await readApprovedFile(file);
    if (createHash('sha256').update(bytes).digest('hex') !== expectedSha256) throw invalid();
    const value: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
    if (!closed(value, ['profile', 'gateway'])) throw invalid();
    return Object.freeze({ profile: readProfile(value.profile), gateway: readGateway(value.gateway) });
  } catch {
    throw invalid();
  }
}
