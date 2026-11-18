import 'server-only';

import {
  configureAuthkitAdapter,
  createAuthkitAdapter,
  createWorkOSProvider,
  IdentityFailure,
  type AuthkitAdapter,
} from '../../lib/server/authkit-adapter.js';
import {
  createPrivateBrowserPorts,
  type PrivateBrowserClientOptions,
} from '../../lib/server/private-browser-client.js';

export type AccountEntryStartupOptions = Omit<Parameters<typeof createAuthkitAdapter>[0], 'provider'> & {
  workosApiKey: string;
};

export type PrivateAccountEntryStartupOptions = Pick<AccountEntryStartupOptions, 'profile' | 'workosApiKey' | 'now'> & {
  privateIdentity: PrivateBrowserClientOptions;
};

/** Install the private pilot only after trusted startup supplies its binding and signing transport. */
export function configurePrivateAccountEntry({
  privateIdentity,
  ...startup
}: PrivateAccountEntryStartupOptions): AuthkitAdapter {
  const ports = createPrivateBrowserPorts(privateIdentity);
  return configureAccountEntry({ ...startup, ...ports });
}

/** Trusted server startup supplies the configured profile, secret, and durable ports. */
export function configureAccountEntry({
  workosApiKey,
  ...ports
}: AccountEntryStartupOptions): AuthkitAdapter {
  if (typeof workosApiKey !== 'string' || workosApiKey.trim() === '') {
    throw new IdentityFailure('IDENTITY_CONFIGURATION_INVALID', 503);
  }

  const provider = createWorkOSProvider(ports.profile, workosApiKey, { now: ports.now });
  const adapter = createAuthkitAdapter({ ...ports, provider });
  configureAuthkitAdapter(adapter);
  return adapter;
}
