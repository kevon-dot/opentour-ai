import 'server-only';

import { AsyncLocalStorage } from 'node:async_hooks';
import { channel } from 'node:diagnostics_channel';

const phases = channel('opentour:bff-readiness:v1');
const observingProbe = new AsyncLocalStorage<true>();

type StartupPhase = 'runtime_started' | 'workos_key_shape_validated' | 'binding_validated'
  | 'role_provider_configured' | 'runtime_configured';
type ProbePhase = 'probe_started' | 'credentials_validated' | 'signed_request_prepared'
  | 'probe_accepted' | 'probe_refused';

/** Publish fixed phases only when a trusted process-local observer is attached. */
export function observeBffStartup(phase: StartupPhase): void {
  if (phases.hasSubscribers) phases.publish(phase);
}

export function withBffProbeObservation(check: () => Promise<boolean>): Promise<boolean> {
  return phases.hasSubscribers ? observingProbe.run(true, check) : check();
}

export function observeBffProbe(phase: ProbePhase): void {
  if (observingProbe.getStore()) phases.publish(phase);
}
