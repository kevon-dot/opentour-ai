import type { BlockingReason, BlockingReasonsProps, NextResponsibility } from './blocking-reasons';

const three: BlockingReasonsProps = {
  reasons: [
    { key: 'source', title: 'Source · Timestamp meaning not verified', observation: 'source-check-042 · 09:42 UTC',
      recovery: { label: 'Open source correction', href: '/fixture/source-correction' } },
    { key: 'proof', title: 'Evidence · Independent proof insufficient', observation: 'proof-preflight-042 · 09:42 UTC',
      recovery: { label: 'View proof assessment', href: '/fixture/proof-assessment' } },
    { key: 'release', title: 'Release · Promotion authority missing', observation: 'release-access-042 · 09:42 UTC',
      recovery: { label: 'Review release access', href: '/fixture/release-access' } },
  ],
  nextAction: { kind: 'customer_action', detail: 'Correct the source timestamp mapping.',
    observation: 'source-check-042 · 16 Nov 2026, 09:42 UTC', nextScheduledCheck: '16 Nov 2026, 09:45 UTC' },
};
const kinds: NextResponsibility[] = ['automatic', 'customer_action', 'operator_action', 'waiting_external', 'none'];
const many: BlockingReason[] = Array.from({ length: 30 }, (_, index) => ({
  key: 'item-' + index, title: 'Generated prerequisite ' + (index + 1), responsibility: kinds[index % kinds.length],
  recovery: { label: 'Inspect prerequisite ' + (index + 1), href: '/fixture/prerequisite/' + (index + 1) },
}));
export const blockerFixtures = {
  three,
  one: { reasons: [{ key: 'external', title: 'External assessment awaiting its current observation. ' + 'Reference-'.repeat(12),
    responsibility: 'waiting_external' }], nextAction: { kind: 'waiting_external' } },
  many: { reasons: many, nextAction: { kind: 'automatic' } },
  reduced: { reasons: [three.reasons[2], three.reasons[0]], nextAction: { kind: 'operator_action' } },
  none: { reasons: [], nextAction: { kind: 'none' } },
} as const satisfies Record<string, BlockingReasonsProps>;
export type BlockerFixture = keyof typeof blockerFixtures;
