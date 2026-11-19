import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, realpath, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { it, mock, type TestContext } from 'node:test';
import type { BffRuntimeBinding } from './bff-runtime-binding.js';

mock.module('server-only', { namedExports: {} });
const { readBffRuntimeBinding } = await import('./bff-runtime-binding.js');

const binding: BffRuntimeBinding = {
  profile: {
    origin: 'https://app.example.invalid',
    callbackURI: 'https://app.example.invalid/auth/callback',
    signedOutPath: '/signed-out',
    returnPaths: ['/', '/signed-out'],
    providerOrigins: ['https://api.workos.com'],
    issuer: 'https://identity.example.invalid/',
    audience: 'https://app.example.invalid',
    jwksURL: 'https://api.workos.com/sso/jwks/client_fixture',
    clientId: 'client_fixture',
  },
  gateway: {
    approvedEndpoint: 'https://fixtureapi-vpce-01234567.execute-api.us-east-2.amazonaws.com/dev',
    region: 'us-east-2',
    restApiId: 'fixtureapi',
    vpcEndpointId: 'vpce-01234567',
    taskRoleArn: 'arn:aws:iam::000000000000:role/dev/service-bff',
  },
};
const prettyBytes = Buffer.from(JSON.stringify(binding, null, 2) + '\n');
const digest = (bytes: Buffer) => createHash('sha256').update(bytes).digest('hex');
const invalid = {
  code: 'IDENTITY_CONFIGURATION_INVALID', status: 503, retryable: false,
  message: 'IDENTITY_CONFIGURATION_INVALID',
};

async function writeFixture(t: TestContext, bytes = prettyBytes) {
  const directory = await mkdtemp(join(await realpath(tmpdir()), 'opentour-bff-binding-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const file = join(directory, 'binding.json');
  await writeFile(file, bytes, { flag: 'wx', mode: 0o400 });
  return { directory, file, sha256: digest(bytes) };
}

it('loads the immutable binding using the digest of the original formatted bytes', async t => {
  const { file, sha256 } = await writeFixture(t);
  const actual = await readBffRuntimeBinding(file, sha256);
  assert.deepEqual(actual, binding);
  assert.ok(Object.isFrozen(actual) && Object.isFrozen(actual.gateway) && Object.isFrozen(actual.profile));
  assert.ok(Object.isFrozen(actual.profile.providerOrigins) && Object.isFrozen(actual.profile.returnPaths));
  await assert.rejects(readBffRuntimeBinding(file, digest(Buffer.from(JSON.stringify(binding)))), invalid);
});

it('requires a canonical absolute file and a separately supplied lowercase SHA-256', async t => {
  const { file, directory, sha256 } = await writeFixture(t);
  await assert.rejects(readBffRuntimeBinding(undefined, sha256), invalid);
  await assert.rejects(readBffRuntimeBinding('binding.json', sha256), invalid);
  await assert.rejects(readBffRuntimeBinding(`${directory}/./binding.json`, sha256), invalid);
  await assert.rejects(readBffRuntimeBinding(file, undefined), invalid);
  await assert.rejects(readBffRuntimeBinding(file, sha256.toUpperCase()), invalid);
  await assert.rejects(readBffRuntimeBinding(file, sha256 + '\n'), invalid);
});

it('refuses final-component and parent symlinks, directories, and missing files', async t => {
  const { directory, file, sha256 } = await writeFixture(t);
  const fileLink = join(directory, 'file-link');
  const directoryLink = join(directory, 'directory-link');
  await symlink(file, fileLink);
  await symlink(directory, directoryLink);
  await assert.rejects(readBffRuntimeBinding(fileLink, sha256), invalid);
  await assert.rejects(readBffRuntimeBinding(join(directoryLink, 'binding.json'), sha256), invalid);
  await assert.rejects(readBffRuntimeBinding(directory, sha256), invalid);
  await assert.rejects(readBffRuntimeBinding(join(directory, 'missing.json'), sha256), invalid);
});

it('refuses an oversized file and invalid UTF-8 even with a matching digest', async t => {
  const oversized = await writeFixture(t, Buffer.concat([Buffer.alloc(16_385, 32), prettyBytes]));
  await assert.rejects(readBffRuntimeBinding(oversized.file, oversized.sha256), invalid);
  const malformed = await writeFixture(t, Buffer.of(0xff));
  await assert.rejects(readBffRuntimeBinding(malformed.file, malformed.sha256), invalid);
});

it('rejects open or wrongly typed bindings, unrelated gateway destinations, and invalid IAM roles', async t => {
  const cases = [
    [],
    { ...binding, workosApiKey: 'fixture' },
    { ...binding, profile: { ...binding.profile, extra: true } },
    { ...binding, profile: { ...binding.profile, jwksURL: undefined } },
    { ...binding, profile: { ...binding.profile, returnPaths: ['/', 1] } },
    { ...binding, profile: { ...binding.profile, callbackURI: binding.profile.origin + '/other' } },
    { ...binding, gateway: { ...binding.gateway, extra: 'fixture' } },
    { ...binding, gateway: { ...binding.gateway, region: 'us-west-2' } },
    { ...binding, gateway: { ...binding.gateway, approvedEndpoint: binding.gateway.approvedEndpoint + '/' } },
    { ...binding, gateway: { ...binding.gateway, taskRoleArn: 'arn:aws:iam::000000000000:user/dev' } },
  ];
  for (const value of cases) {
    const { file, sha256 } = await writeFixture(t, Buffer.from(JSON.stringify(value)));
    await assert.rejects(readBffRuntimeBinding(file, sha256), invalid);
  }
});
