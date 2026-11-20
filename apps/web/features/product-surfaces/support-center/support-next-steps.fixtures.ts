import type { SupportNextStepsProps } from './support-next-steps';

const case41 = {
  supportCase: { resource: 'Support case 41', state: 'Open', nextStep: 'Await owner assignment', owner: null, nextUpdate: null },
  incident: { resource: 'Related incident', state: 'Current incident state', nextStep: 'Follow operational next step' },
  additionalGrant: { resource: 'Additional grant', state: 'No current permission', nextStep: 'Customer reviews an explicit current request' },
} as const;

export const supportFixtures = {
  case41,
  closed: {
    supportCase: { resource: 'Support case 41', state: 'Closed', nextStep: 'Read the closure receipt', owner: 'Generated support owner', nextUpdate: null },
    incident: { resource: 'Related incident', state: 'Investigating', nextStep: 'Check the next incident update' },
    additionalGrant: { resource: 'Additional grant', state: 'Expired', nextStep: 'Customer reviews an explicit current request' },
    enforcement: 'Access removal pending',
  },
  restricted: { supportCase: null, incident: null, additionalGrant: case41.additionalGrant },
  cleared: { supportCase: null, incident: null, additionalGrant: null },
  previous: {
    supportCase: { resource: 'fixture-private-case-41', state: 'fixture-private-case-state', nextStep: 'fixture-private-case-step',
      owner: 'fixture-private-owner', nextUpdate: 'fixture-private-update' },
    incident: { resource: 'fixture-private-incident', state: 'fixture-private-incident-state', nextStep: 'fixture-private-incident-step' },
    additionalGrant: { resource: 'fixture-private-grant', state: 'fixture-private-grant-state', nextStep: 'fixture-private-grant-step' },
    enforcement: 'fixture-private-enforcement',
  },
  long: {
    ...case41,
    incident: { resource: 'Incident-' + 'long-reference-'.repeat(12), state: 'Investigation remains open',
      nextStep: 'Review ' + 'unbrokenevidence'.repeat(14) },
  },
} as const satisfies Record<string, SupportNextStepsProps>;
export type SupportFixture = keyof typeof supportFixtures;
