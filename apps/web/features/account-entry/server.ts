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
import {
  createPrivateGatewaySigner,
  type PrivateGatewaySignerOptions,
} from '../../lib/server/private-gateway-signer.js';

export type AccountEntryStartupOptions = Omit<Parameters<typeof createAuthkitAdapter>[0], 'provider'> & {
  workosApiKey: string;
};

type AccountEntrySettings = Pick<AccountEntryStartupOptions, 'profile' | 'workosApiKey' | 'now'>;

export type PrivateAccountEntryStartupOptions = AccountEntrySettings & {
  privateIdentity: PrivateBrowserClientOptions;
};

export type SignedPrivateAccountEntryStartupOptions = AccountEntrySettings & {
  privateIdentity: Omit<PrivateGatewaySignerOptions, 'now'> & Pick<PrivateBrowserClientOptions, 'fetch'>;
};

/** Trusted Node startup supplies the reviewed gateway binding, workload credentials and transport. */
export function configureSignedPrivateAccountEntry({
  privateIdentity: { fetch, ...binding },
  ...startup
}: SignedPrivateAccountEntryStartupOptions): AuthkitAdapter & {
  checkReadiness: (signal: AbortSignal) => Promise<boolean>;
} {
  const sign = createPrivateGatewaySigner({ ...binding, now: startup.now });
  const { checkReadiness, ...ports } = createPrivateBrowserPorts({ approvedEndpoint: binding.approvedEndpoint,
    sign, fetch });
  const adapter = configureAccountEntry({ ...startup, ...ports });
  return { ...adapter, checkReadiness };
}

/** Install the private pilot only after trusted startup supplies its binding and signing transport. */
export function configurePrivateAccountEntry({
  privateIdentity,
  ...startup
}: PrivateAccountEntryStartupOptions): AuthkitAdapter {
  const { transactions, sessions, selections } = createPrivateBrowserPorts(privateIdentity);
  return configureAccountEntry({ ...startup, transactions, sessions, selections });
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
