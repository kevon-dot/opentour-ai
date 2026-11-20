import type { ActivityEvent, ActivitySnapshot } from './activity-history';

export const referenceEvents: readonly ActivityEvent[] = [
  ['09:42:30', 'Evidence reconstruction advanced', 'reconstruct-015', 'EXACT_JOIN_PROGRESS'],
  ['09:42:25', 'Label verification advanced', 'labels-015', 'RULE_CHECK_PROGRESS'],
  ['09:40:55', 'Cycle admitted', 'cycle-015', 'CYCLE_ADMITTED'],
  ['09:38:05', 'Prior candidate held', 'assessment-014', 'ASSESSMENT_HELD'],
].map(([time, event, receipt, reason]) => ({
  id: receipt, time: time + ' UTC', timestamp: '16 Nov 2026, ' + time + ' UTC', event, receipt, reason,
  actor: 'System', operation: receipt, detail: { label: 'View receipt', href: '/receipts/' + receipt },
}));

const older = [
  { ...referenceEvents[0], id: 'fixture-older-1', event: 'Prior cycle opened' },
  { ...referenceEvents[1], id: 'fixture-older-2', event: 'Source observed' },
];
export type ActivityFixture = 'reference' | 'appended' | 'exhausted' | 'replacement' | 'read-only';
export function activityFixture(state: ActivityFixture): ActivitySnapshot {
  if (state === 'read-only') return { generation: 'viewer', exhausted: true, readOnly: true, events: [{
    id: 'public-1', time: '09:45:00 UTC', timestamp: '16 Nov 2026, 09:45:00 UTC',
    event: 'Published activity ' + 'long-observation'.repeat(8), reason: 'Visible summary only',
  }] };
  if (state === 'replacement') {
    const current = { ...older[0], id: 'fixture-new', event: 'New snapshot event', receipt: 'current-receipt',
      operation: 'current-operation', detail: { label: 'View receipt', href: '/receipts/current-receipt' } };
    return { generation: 'new', readOnly: false, exhausted: false, events: [current, current] };
  }
  const events = state === 'reference' ? referenceEvents :
    [...referenceEvents, referenceEvents[3], ...older.slice(0, state === 'appended' ? 1 : 2)];
  return { generation: 'reference', events, readOnly: false, exhausted: state === 'exhausted' };
}
