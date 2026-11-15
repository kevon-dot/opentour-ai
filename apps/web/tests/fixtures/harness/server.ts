import { createServer, type ServerResponse, type IncomingMessage } from 'node:http';
import { readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { stripTypeScriptTypes } from 'node:module';
import type { Socket } from 'node:net';
import { FIXTURE, SCENARIOS, type CanonicalError, type Projection, type Session, type Scenario, type Mode } from './contracts.ts';

const files = new URL('./', import.meta.url);
const common = JSON.parse(readFileSync(new URL('../../../../../packages/contracts/common/common-contract.schema.json', import.meta.url), 'utf8'));
const revisionPattern = new RegExp(common.$defs.ResourceRevision.pattern);
const isRevision = (value: unknown): value is string => typeof value === 'string' && /^[0-9]+$/.test(value) && revisionPattern.test(value);
function json(res: ServerResponse, status: number, body: unknown) {
  res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' });
  res.end(JSON.stringify(body));
}
function error(res: ServerResponse, status: number, code: string, message: string, revision: string | null = null) {
  const body: CanonicalError = { code, message, request_id: 'simulated-request-127', retryable: status === 503 || status === 429, field_errors: code === 'SCHEMA_INVALID' ? [{ field: 'expected_revision', code: 'INVALID_RESOURCE_REVISION' }] : [], current_revision: revision };
  json(res, status, body);
}
async function body(req: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = []; let size = 0;
  for await (const chunk of req) { size += chunk.length; if (size > 16_384) throw new Error('body_limit'); chunks.push(chunk); }
  const value: unknown = JSON.parse(Buffer.concat(chunks).toString() || '{}');
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('body_shape');
  return value as Record<string, unknown>;
}
export async function startFixtureServer(runId: string, cleanupObservationFault = false) {
  const sessions = new Map<string, Session>();
  const sockets = new Set<Socket>();
  const client = stripTypeScriptTypes(readFileSync(new URL('client.ts', files), 'utf8'), { mode: 'strip' });
  let origin = '';
  const server = createServer((req, res) => { void handle(req, res).catch(() => { if (!res.headersSent) error(res, 422, 'SCHEMA_INVALID', 'Invalid fixture request.'); else res.destroy(); }); });
  server.on('connection', socket => { sockets.add(socket); socket.once('close', () => sockets.delete(socket)); });
  async function handle(req: IncomingMessage, res: ServerResponse) {
    if (req.headers.host !== new URL(origin).host) { error(res, 403, 'CAPABILITY_DENIED', 'Fixture host denied.'); return; }
    const url = new URL(req.url ?? '/', origin);
    if (url.pathname === '/__harness/ready') { json(res, 200, { run_id: runId, fixture: FIXTURE }); return; }
    if (req.headers.origin && req.headers.origin !== origin) { error(res, 403, 'CAPABILITY_DENIED', 'Fixture origin denied.'); return; }
    if (url.pathname === '/__harness/session' && req.method === 'POST') {
      const input = await body(req); const mode = input.mode as Mode; const scenario = (input.scenario ?? 'success') as Scenario;
      if (!['customer_controlled', 'opentour_managed'].includes(mode) || !SCENARIOS.includes(scenario)) throw new Error('fixture_input');
      const id = randomUUID();
      sessions.set(id, { id, mode, scenario, organization: 'simulated-org-a', revision: '9007199254740993', authority: scenario !== 'denied', role: input.role === 'viewer' ? 'viewer' : 'owner', effects: 0, requests: 0, operation: null, intents: new Map(), delayed: false, held: [], closed: false });
      json(res, 201, { id }); return;
    }
    if (url.pathname === '/client.js') { res.writeHead(200, { 'content-type': 'text/javascript', 'cache-control': 'no-store' }); res.end(client); return; }
    if (url.pathname === '/font.woff2') { res.writeHead(200, { 'content-type': 'font/woff2' }); res.end(readFileSync(new URL('assets/inter-latin-400-normal.woff2', files))); return; }
    if (url.pathname === '/favicon.ico') { res.writeHead(204); res.end(); return; }
    if (url.pathname === '/harness') { res.writeHead(200, { 'content-type': 'text/html', 'cache-control': 'no-store', 'content-security-policy': "default-src 'self'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; font-src 'self'; img-src 'self' data:; frame-src 'none'; object-src 'none'; base-uri 'none'" }); res.end(readFileSync(new URL('index.html', files))); return; }
    const match = /^\/(control|cell|__harness)\/([^/]+)\/(state|switch|mutate|revoke|reconcile|configure|release|inspect|destroy)$/.exec(url.pathname);
    const session = match && sessions.get(match[2]);
    if (!match || !session || session.closed) { error(res, 404, 'RESOURCE_NOT_FOUND', 'Resource unavailable.'); return; }
    const action = match[3];
    if (action === 'configure') {
      const input = await body(req);
      if (input.scenario && !SCENARIOS.includes(input.scenario as Scenario)) throw new Error('scenario');
      if (input.scenario) session.scenario = input.scenario as Scenario;
      if (typeof input.authority === 'boolean') session.authority = input.authority;
      if (typeof input.delayed === 'boolean') session.delayed = input.delayed;
      if (isRevision(input.revision)) session.revision = input.revision;
      json(res, 200, { ok: true }); return;
    }
    if (action === 'release') { session.held.splice(0).forEach(release => release()); json(res, 200, { ok: true }); return; }
    if (action === 'inspect') { json(res, 200, { effects: session.effects, requests: session.requests, held: session.held.length, organization: session.organization, revision: session.revision, operation: session.operation }); return; }
    if (action === 'destroy') { session.closed = true; session.held.splice(0).forEach(release => release()); sessions.delete(session.id); json(res, 200, { ok: true }); return; }
    if (action === 'switch') {
      const input = await body(req);
      if (input.organization !== 'simulated-org-b' || !session.authority) { error(res, 404, 'RESOURCE_NOT_FOUND', 'Resource unavailable.'); return; }
      session.organization = 'simulated-org-b'; json(res, 200, projection(session)); return;
    }
    if (action === 'state' || action === 'reconcile') {
      session.requests++;
      if (match[1] === 'cell' && session.scenario === 'offline') { error(res, 503, 'CELL_UNREACHABLE', 'Connect to your private network and retry the cell connection.'); return; }
      if (session.scenario === 'error') { error(res, 403, 'CAPABILITY_DENIED', '<img src=x onerror=alert(1)> Access denied.'); return; }
      if (session.scenario === 'rate_limit' || session.scenario === 'unavailable') { error(res, session.scenario === 'rate_limit' ? 429 : 503, session.scenario === 'rate_limit' ? 'RATE_LIMITED' : 'AUTHORITY_UNAVAILABLE', 'Service is temporarily unavailable. Review the connection and retry.'); return; }
      if (session.scenario === 'malformed') { json(res, 200, { state: 'made_up_success', private_diagnostic: 'simulated-private-diagnostic' }); return; }
      const data = projection(session);
      if ((session.delayed && session.organization === 'simulated-org-a') || session.scenario === 'loading') { session.held.push(() => { if (!res.destroyed) json(res, 200, data); }); return; }
      json(res, 200, data); return;
    }
    const input = await body(req);
    if (Object.keys(input).some(key => !['expected_revision', 'intent', 'value', 'resource'].includes(key)) || !isRevision(input.expected_revision) || typeof input.intent !== 'string' || typeof input.value !== 'string') { error(res, 422, 'SCHEMA_INVALID', 'Review the highlighted revision field.'); return; }
    if (input.resource !== session.organization) { error(res, 404, 'RESOURCE_NOT_FOUND', 'Resource unavailable.'); return; }
    if (!session.authority || session.role === 'viewer') { error(res, 403, 'CAPABILITY_DENIED', 'Current authority does not permit this action.'); return; }
    const digest = JSON.stringify({ action, ...input }); const prior = session.intents.get(input.intent);
    if (prior) { if (prior.body !== digest) error(res, 409, 'IDEMPOTENCY_CONFLICT', 'This intent was already used with different content.'); else json(res, 200, { revision: prior.revision, operation_id: session.operation }); return; }
    if (input.expected_revision !== session.revision || session.scenario === 'stale') { error(res, 409, 'STALE_REVISION', 'State changed. Review your draft before submitting a new intent.', session.revision); return; }
    if (session.revision === '9223372036854775807') { error(res, 409, 'REVISION_EXHAUSTED', 'Revision limit reached.'); return; }
    session.revision = (BigInt(session.revision) + 1n).toString(); session.effects++;
    if (action === 'revoke') { session.operation = 'simulated-revocation-127'; session.scenario = 'pending'; session.authority = false; }
    session.intents.set(input.intent, { body: digest, revision: session.revision });
    if (session.scenario === 'lost_ack') { res.destroy(); return; }
    json(res, action === 'revoke' ? 202 : 200, { revision: session.revision, operation_id: session.operation });
  }
  await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', () => { server.off('error', reject); resolve(); }); });
  const address = server.address(); if (!address || typeof address === 'string') throw new Error('fixture_listen');
  origin = `http://127.0.0.1:${address.port}`;
  return { origin, async close() {
    sessions.forEach(s => { s.closed = true; s.held.splice(0).forEach(release => release()); }); sessions.clear();
    const socketClosures = [...sockets].map(socket => new Promise<void>(resolve => socket.once('close', () => resolve())));
    const closed = new Promise<void>(resolve => server.close(() => resolve()));
    sockets.forEach(socket => socket.destroy()); await closed; await Promise.all(socketClosures);
    if (cleanupObservationFault) throw new Error('Injected cleanup observation failure after resource closure.');
    return !server.listening && sockets.size === 0;
  } };
}
function projection(s: Session): Projection {
  return { kind: 'simulated', mode: s.mode, organization_id: s.organization, role: s.role, authority_revision: s.authority ? FIXTURE.authority_revision : null, revision: s.revision, event_sequence: 1, state: s.scenario, label: s.organization === 'simulated-org-a' ? 'Generated workspace A' : 'Generated workspace B', operation_id: s.operation, revocation_state: s.scenario === 'pending' ? 'pending_cells' : s.scenario === 'unknown' || s.scenario === 'lost_ack' ? 'reconciliation_required' : 'confirmed', affected_cells: 2, acknowledged_cells: s.scenario === 'pending' ? 1 : 2, unresolved_effects: s.scenario === 'unknown' ? null : 0, quantity: s.scenario === 'unknown' ? null : '12.50', amount_minor: s.scenario === 'unknown' ? null : 1250 };
}
