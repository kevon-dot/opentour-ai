import type { ObservedCount } from './chart-evidence.types';
import type { PairedOutcomesProps } from './paired-outcomes';

const observed = (count: number): ObservedCount => ({ status: 'observed', count });
const notSupplied = { status: 'unknown', reason: 'Count not supplied' } as const;

export const pairedFixtures = {
  complete: {
    expectedPairs: 200, completePairs: 200, missingCandidate: observed(0), excluded: observed(0), unknown: observed(0),
    outcomes: { bothCorrect: observed(130), fixed: observed(40), regressed: observed(10), bothIncorrect: observed(20) },
  },
  partial: {
    expectedPairs: 200, completePairs: 180, missingCandidate: observed(20), excluded: notSupplied, unknown: notSupplied,
    outcomes: { bothCorrect: notSupplied, fixed: notSupplied, regressed: notSupplied, bothIncorrect: notSupplied },
  },
  mixed: {
    expectedPairs: 10, completePairs: 6, missingCandidate: observed(2), excluded: observed(1), unknown: observed(1),
    outcomes: { bothCorrect: observed(4), fixed: observed(0), regressed: notSupplied,
      bothIncorrect: { status: 'missing', reason: 'Result unavailable' } },
  },
} as const satisfies Record<string, PairedOutcomesProps>;
