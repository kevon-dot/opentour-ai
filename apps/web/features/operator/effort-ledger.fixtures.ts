import type { EffortLedgerEntry } from './effort-ledger';

export const referenceEntries: readonly EffortLedgerEntry[] = [
  { id: 'effort-001', category: 'Failed work', person: 'Jordan Davis', actorClass: 'Provider', sourceQuality: 'Reported', activeMinutes: 18, waitingMinutes: 0 },
  { id: 'effort-002', category: 'External wait', person: 'Jordan Davis', actorClass: 'Provider', sourceQuality: 'Reported', activeMinutes: 0, waitingMinutes: 42 },
  { id: 'effort-003', category: 'Review', person: 'Mina Patel', actorClass: 'Customer', sourceQuality: 'Reported', activeMinutes: 12, waitingMinutes: 0 },
];

export const historyEntries: readonly EffortLedgerEntry[] = [
  { ...referenceEntries[0], interval: { startUtc: '2026-11-16 09:00', endUtc: '2026-11-16 09:18' }, workItem: 'work-042' },
  referenceEntries[1],
  referenceEntries[2],
  { id: 'effort-016', category: 'Deployment', person: 'Fixture specialist', actorClass: 'Provider', sourceQuality: 'Timer', activeMinutes: 60, waitingMinutes: 0 },
  { id: 'effort-012', category: 'Setup', person: 'Fixture operator', actorClass: 'Provider', sourceQuality: 'Imported', activeMinutes: null, waitingMinutes: 0,
    interval: { startUtc: '2026-11-16 11:00', endUtc: null } },
  { id: 'effort-009', category: 'Change', person: 'Fixture specialist', actorClass: 'Provider', sourceQuality: 'Imported', activeMinutes: 4, waitingMinutes: null },
  { ...referenceEntries[0], id: 'effort-017', activeMinutes: 16, supersedes: 'effort-001' },
];
