/** Test-only control/cell projections; these are not production endpoints or receipts. */
export type Mode = 'customer_controlled' | 'opentour_managed';
export type Scenario = 'success' | 'loading' | 'empty' | 'pending' | 'unknown' | 'denied' | 'error' | 'offline' | 'stale' | 'lost_ack' | 'malformed' | 'rate_limit' | 'unavailable';
export type CanonicalError = { code: string; message: string; request_id: string; retryable: boolean; field_errors: Array<{ field: string; code: string }>; current_revision: string | null };
export type Projection = {
  kind: 'simulated'; mode: Mode; organization_id: string; role: 'owner' | 'viewer';
  authority_revision: string | null; revision: string; event_sequence: number;
  state: Scenario; label: string; operation_id: string | null;
  revocation_state: 'pending_cells' | 'draining' | 'confirmed' | 'reconciliation_required';
  affected_cells: number; acknowledged_cells: number; unresolved_effects: number | null;
  quantity: string | null; amount_minor: number | null;
};
export type Session = {
  id: string; mode: Mode; scenario: Scenario; organization: string; revision: string;
  authority: boolean; role: 'owner' | 'viewer'; effects: number; requests: number;
  operation: string | null; intents: Map<string, { body: string; revision: string }>;
  delayed: boolean; held: Array<() => void>; closed: boolean;
};
export const FIXTURE = Object.freeze({
  id: 'simulated-browser-harness', version: '1', seed: 127,
  clock: '2026-11-14T12:00:00.000Z', authority_revision: 'simulated-authority-1',
  source_issue: 'OPE-127', source_revision: '2026-11-15T05:00:07.626Z',
  spec_sha256: '96c405228e6e925e3c11ea4d44c0d5cb5b984541bab1a017735e7629ffa72292',
});
export const SCENARIOS: Scenario[] = ['success', 'loading', 'empty', 'pending', 'unknown', 'denied', 'error', 'offline', 'stale', 'lost_ack', 'malformed', 'rate_limit', 'unavailable'];
