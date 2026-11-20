import type { VerifierQualificationProps } from './verifier-qualification';

const exact: VerifierQualificationProps = {
  revision: 'Verifier revision 4',
  slices: [
    { key: 'audit', label: 'Known-answer audit', result: 'Passed', reason: 'Current exact fixture manifest' },
    { key: 'risk', label: 'Critical-risk slice', result: 'Unresolved', reason: 'Insufficient current evidence' },
    { key: 'protected', label: 'Protected partition', result: 'Not evaluated', reason: 'Separate grant required', comparison: { disclosure: 'withheld' } },
  ],
  impacts: [
    { key: 'revision', resource: 'Verifier revision 4', state: 'Current', nextStep: 'Inspect qualification' },
    { key: 'labels', resource: 'Labels from revision 3', state: 'Historical', nextStep: 'Do not silently relabel' },
    { key: 'datasets', resource: 'Affected derived datasets', state: 'Unknown', nextStep: 'Inspect exact impact result' },
  ],
};
export const verifierFixtures = {
  exact,
  measured: { ...exact, slices: [
    { ...exact.slices[0], comparison: { disclosure: 'authorized', samples: 200, agreement: '197 / 200', disagreement: '3 / 200' } },
    { ...exact.slices[1], comparison: { disclosure: 'authorized', samples: 0 } },
    { key: 'scoped', label: 'Generated comparison A', result: 'Failed', reason: 'Qualification operation failed', comparison: {
      disclosure: 'authorized', samples: 731, agreement: '731 / 731', disagreement: '0 / 731',
    } },
  ] },
  replacement: { revision: 'Verifier revision 5', slices: [
    { key: 'latest', label: 'Current audit', result: 'Not evaluated', reason: 'No current result supplied' },
  ], impacts: [
    { key: 'old', resource: 'Labels from revision 3', state: 'Historical', nextStep: 'Do not silently relabel' },
    { key: 'previous', resource: 'Labels from revision 4', state: 'Historical', nextStep: 'Inspect revision 4 evidence' },
  ] },
} as const satisfies Record<string, VerifierQualificationProps>;
export type VerifierFixture = keyof typeof verifierFixtures;
