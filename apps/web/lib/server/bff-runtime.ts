import 'server-only';

import { configureSignedPrivateAccountEntry } from '../../features/account-entry/server.js';
import { readBffRuntimeBinding } from './bff-runtime-binding.js';
import { createEcsTaskCredentialProvider, fetchEcsTaskCredentials } from './ecs-task-role.js';

const probeTimeoutMs = 3_000;
const probeIntervalMs = 10_000;
type Check = (signal: AbortSignal) => Promise<boolean>;
type Runtime = { startup?: Promise<void>; ready?: () => Promise<boolean> };
const runtimeKey = Symbol.for('@opentour/web.bff-runtime');
const runtimes = globalThis as typeof globalThis & { [runtimeKey]?: Runtime };

function cachedReadiness(check: Check): () => Promise<boolean> {
  let last: { at: number; ready: boolean } | undefined;
  let pending: Promise<boolean> | undefined;
  return () => {
    if (pending) return pending;
    const now = performance.now();
    if (last && now >= last.at && now - last.at < probeIntervalMs) return Promise.resolve(last.ready);
    pending = check(AbortSignal.timeout(probeTimeoutMs)).catch(() => false).then(ready => {
      last = { at: performance.now(), ready };
      pending = undefined;
      return ready;
    });
    return pending;
  };
}

async function initialize(runtime: Runtime): Promise<void> {
  try {
    const workosApiKey = process.env.OPENTOUR_WORKOS_API_KEY;
    if (!workosApiKey || workosApiKey.length > 4_096 || workosApiKey.trim() !== workosApiKey) return;
    const { profile, gateway: { taskRoleArn, ...binding } } = await readBffRuntimeBinding(
      process.env.OPENTOUR_BFF_BINDING_FILE,
      process.env.OPENTOUR_BFF_BINDING_SHA256,
    );
    const credentials = createEcsTaskCredentialProvider({
      relativeUri: process.env.AWS_CONTAINER_CREDENTIALS_RELATIVE_URI,
      expectedRoleArn: taskRoleArn,
      fetch: fetchEcsTaskCredentials,
    });
    const { checkReadiness } = configureSignedPrivateAccountEntry({
      profile,
      workosApiKey,
      privateIdentity: { ...binding, credentials, fetch: globalThis.fetch },
    });
    runtime.ready = cachedReadiness(checkReadiness);
  } catch {
    // Keep the routing plane closed when the deployment supplies incomplete or invalid inputs.
  }
}

/** Called once per Node server process. Building the application needs no runtime configuration. */
export function startBffRuntime(): Promise<void> {
  const runtime = runtimes[runtimeKey] ??= {};
  return runtime.startup ??= initialize(runtime);
}

export async function bffRuntimeReady(): Promise<boolean> {
  const ready = runtimes[runtimeKey]?.ready;
  return ready ? ready() : false;
}
