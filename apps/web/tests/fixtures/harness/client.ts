import type { Projection, CanonicalError } from './contracts.ts';
const element = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const session = new URL(location.href).searchParams.get('session');
let generation = 0;
let current: Projection | null = null;
let intent: { expected_revision: string; intent: string; value: string; resource: string } | null = null;
let view: 'workspace' | 'billing' = 'workspace';
let intentSequence = 0;
const button = (id: string) => element<HTMLButtonElement>(id);
const input = (id: string) => element<HTMLInputElement>(id);
const setText = (id: string, value: string) => { element(id).textContent = value; };
function status(value: string, detail = '') { setText('status', value); setText('detail', detail); document.body.dataset.state = value; }
function deny() { button('save').disabled = true; button('revoke').disabled = true; }
function clearError() { setText('error', ''); setText('field-error', ''); input('expected').removeAttribute('aria-invalid'); }
function showError(value: unknown) {
  deny();
  if (value && typeof value === 'object' && typeof (value as CanonicalError).message === 'string' && typeof (value as CanonicalError).code === 'string' && typeof (value as CanonicalError).request_id === 'string') {
    const error = value as CanonicalError;
    setText('error', `${error.message} Request: ${error.request_id}`);
    status(error.code === 'STALE_REVISION' ? 'Stale — review required' : 'Action denied or unavailable');
    if (error.field_errors?.some(item => item.field === 'expected_revision')) { input('expected').setAttribute('aria-invalid', 'true'); setText('field-error', 'Use an exact positive decimal string.'); }
  } else { setText('error', 'The response could not be verified. Refresh before taking action.'); status('Unknown'); }
}
function valid(value: unknown): value is Projection {
  if (!value || typeof value !== 'object') return false;
  const p = value as Projection;
  return p.kind === 'simulated' && ['customer_controlled', 'opentour_managed'].includes(p.mode) && typeof p.organization_id === 'string' && typeof p.revision === 'string' && /^(?:[1-9][0-9]{0,18})$/.test(p.revision) && BigInt(p.revision) <= 9223372036854775807n && ['success','loading','empty','pending','unknown','denied','offline','stale','lost_ack'].includes(p.state);
}
function render(data: Projection) {
  current = data;
  setText('mode', data.mode); setText('organization', data.label); setText('role', data.role);
  setText('authority', data.authority_revision ?? 'Unknown'); setText('revision', data.revision);
  if (!input('expected').value) input('expected').value = data.revision;
  const canAct = !!data.authority_revision && data.role === 'owner' && data.state === 'success';
  button('save').disabled = !canAct; button('revoke').disabled = !canAct;
  const labels: Partial<Record<Projection['state'], string>> = { success: 'Ready', empty: 'No items', pending: 'Waiting for cells', unknown: 'Unknown', denied: 'Access denied', stale: 'Stale — review required', lost_ack: 'Unknown' };
  status(labels[data.state] ?? 'Ready', data.state === 'pending' ? `${data.acknowledged_cells} of ${data.affected_cells} cells acknowledged. Enforcement remains pending.` : data.state === 'unknown' || data.state === 'lost_ack' ? 'The outcome is not confirmed. Reconcile the existing intent.' : data.state === 'empty' ? 'No generated items exist in this workspace.' : 'Generated deterministic fixture state.');
  if (data.operation_id) setText('detail', `${element('detail').textContent} Operation: ${data.operation_id}`);
  element('billing-data').hidden = view !== 'billing';
  setText('view-title', view === 'billing' ? 'Simulated billing' : 'Workspace state');
  setText('billing-data', data.amount_minor === null ? 'Simulated amount: unknown · quantity: unknown' : `Simulated amount: $${(data.amount_minor / 100).toFixed(2)} USD · quantity: ${data.quantity}`);
}
async function refresh() {
  const mine = ++generation; clearError(); deny(); status('Loading');
  try {
    const response = await fetch(`/cell/${session}/state`); const data: unknown = await response.json();
    if (mine !== generation) return;
    if (!response.ok) { showError(data); return; }
    if (!valid(data)) { showError(null); return; }
    render(data);
  } catch { if (mine === generation) { status('Unknown'); setText('error', 'Connection unavailable. Open connection help and retry.'); } }
}
async function mutate(action: 'mutate' | 'revoke', replay = false) {
  if (!current) return;
  if (!replay) intent = { expected_revision: input('expected').value, intent: `simulated-intent-${++intentSequence}`, value: input('draft').value, resource: current.organization_id };
  if (!intent) return;
  const mine = generation; clearError(); deny(); status('Request pending');
  try {
    const response = await fetch(`/control/${session}/${action}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(intent) });
    const data = await response.json(); if (mine !== generation) return;
    if (!response.ok) { showError(data); return; }
    button('retry').disabled = false;
    if (typeof data.revision !== 'string') { showError(null); return; }
    input('expected').value = data.revision;
    await refresh();
  } catch { if (mine === generation) { status('Unknown', 'Acknowledgment was lost. Retry only the same intent or refresh its status.'); button('retry').disabled = false; } }
}
button('refresh').onclick = () => { void refresh(); };
button('save').onclick = () => { void mutate('mutate'); };
button('revoke').onclick = () => { void mutate('revoke'); };
button('retry').onclick = () => { void mutate('mutate', true); };
button('workspace').onclick = () => { view = 'workspace'; history.replaceState(null, '', `${location.pathname}?session=${session}#workspace`); if (current) render(current); };
button('billing').onclick = () => { view = 'billing'; history.replaceState(null, '', `${location.pathname}?session=${session}#billing`); if (current) render(current); };
button('switch').onclick = async () => {
  const mine = ++generation; current = null; intent = null; deny(); button('retry').disabled = true;
  setText('organization', 'Switching workspace'); setText('authority', 'Unknown'); setText('revision', 'Unknown'); input('expected').value = ''; status('Loading');
  try { const response = await fetch(`/control/${session}/switch`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ organization: 'simulated-org-b' }) }); const data = await response.json(); if (mine !== generation) return; if (!response.ok || !valid(data)) { showError(data); return; } render(data); } catch { if (mine === generation) showError(null); }
};
button('help').onclick = () => element<HTMLDialogElement>('help-dialog').showModal();
button('close-help').onclick = () => element<HTMLDialogElement>('help-dialog').close();
element('help-dialog').addEventListener('close', () => button('help').focus());
void refresh();
