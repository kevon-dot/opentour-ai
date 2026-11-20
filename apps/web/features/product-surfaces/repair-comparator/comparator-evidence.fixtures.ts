import type { ComparatorAlternative, ComparatorEvidenceProps } from './comparator-evidence';

function alternative(id: string, name: string, effort: string, cost: string, latency: string): ComparatorAlternative {
  return { id, name, effort, costPerCase: { value: cost }, latencyP95: { value: latency },
    errors: { value: '0' }, denominator: { value: '200' }, exclusions: { value: '0' }, samePopulation: 'Yes', sameEvidence: 'Yes' };
}

const baseline = alternative('baseline', 'Current baseline', 'Current operation', 'USD 0.030', '1,200 ms');
const retrieval = alternative('retrieval', 'Retrieval repair', 'Configuration change', 'USD 0.020', '900 ms');
const policy = alternative('policy', 'Policy repair', 'Policy review', 'USD 0.010', '800 ms');
const tied: ComparatorEvidenceProps = {
  frozenIdentity: 'Baseline 3',
  selectionPolicy: 'Development errors on the matched population',
  tieHandling: 'Keep equally scoring alternatives. Report lower cost separately.',
  quality: { kind: 'tied', explanation: 'All three alternatives have zero observed errors. Lower cost is reported separately.' },
  coverage: { state: 'Complete', detail: 'Exact development sample' },
  parity: { state: 'Matched', reason: 'All alternatives use the same population and evidence.' },
  alternatives: [baseline, retrieval, policy],
  recommendation: { result: 'Do not train', reason: 'No demonstrated training advantage', nextStep: 'Prepare exact repair' },
};

export const comparatorFixtures = {
  tied,
  mismatched: {
    ...tied,
    quality: { kind: 'reported', title: 'Evidence needs review', explanation: 'The comparison includes an alternative evaluated on a different population.' },
    coverage: { state: 'Partial', detail: '180 of 200 cases evaluated' },
    parity: { state: 'Mismatched (blocking)', reason: 'The policy repair used a different population.' },
    alternatives: [baseline, retrieval, { ...policy, samePopulation: 'No', errors: { value: '2' },
      denominator: { value: '180' }, exclusions: { value: '20' } }],
    recommendation: { result: 'Defer', reason: 'The alternatives do not share the same population.', nextStep: 'Collect evidence' },
  },
  unknown: {
    ...tied,
    quality: { kind: 'reported', title: 'Comparison incomplete', explanation: 'The retrieval result is unavailable.' },
    coverage: { state: 'Partial', detail: '200 cases expected for retrieval repair' },
    parity: { state: 'Unknown', reason: 'The retrieval evidence revision was not supplied.' },
    alternatives: [baseline, { ...retrieval, sameEvidence: 'Unknown', errors: { unavailable: 'Result not reported' },
      costPerCase: { unavailable: 'Cost observation missing' }, latencyP95: { unavailable: 'Timing sample missing' } }],
    recommendation: { result: 'Defer', reason: 'Evidence or declared tie resolution incomplete', nextStep: 'Collect evidence' },
  },
  train: {
    ...tied,
    quality: { kind: 'reported', title: 'Training assessment available', explanation: 'The source assessment identifies a remaining error pattern.' },
    alternatives: [{ ...baseline, errors: { value: '8' } }, { ...retrieval, errors: { value: '7' } }],
    recommendation: { result: 'Train', reason: 'Declared evidence supports training', nextStep: 'Review bounded training plan' },
  },
} satisfies Record<string, ComparatorEvidenceProps>;
export type ComparatorFixture = keyof typeof comparatorFixtures;
