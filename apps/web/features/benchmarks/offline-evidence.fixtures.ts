import type { HostedBenchmark, OfflineEvidenceProps } from './offline-evidence';

export const retainedBenchmark: HostedBenchmark = {
  benchmark: 'b7 · Sealed', observedAt: 'Nov 12, 09:42 UTC', eligibleCases: 300,
  population: 'Observed · Nov 1–7 · Population receipts-nov-01-07',
};
export type EvidenceFixture = 'available' | 'offline' | 'recovered' | 'missing' | 'local-restricted' | 'restricted';
export function evidenceFixture(state: EvidenceFixture, onRetry: () => void): OfflineEvidenceProps {
  if (state === 'restricted') return { hosted: null, local: { state: 'restricted' } };
  if (state === 'missing') return {
    hosted: { benchmark: null, observedAt: null, eligibleCases: null, population: null }, local: { state: 'offline', onRetry },
  };
  if (state === 'local-restricted') return { hosted: retainedBenchmark, local: { state: 'restricted' } };
  if (state === 'offline') return { hosted: retainedBenchmark, local: { state, onRetry } };
  return { hosted: retainedBenchmark, local: { state: 'available',
    record: state === 'available' ? 'Synthetic local receipt A' : 'Synthetic local receipt B',
    description: state === 'available' ? 'Prior scoped observation' : 'New scoped observation',
  } };
}
