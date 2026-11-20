import assert from 'node:assert/strict';
import { it } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { OfflineEvidence } from './offline-evidence';
import { evidenceFixture, retainedBenchmark } from './offline-evidence.fixtures';

it('retains the separately supplied hosted count, population and observation time while offline', () => {
  const html = renderToStaticMarkup(<OfflineEvidence {...evidenceFixture('offline', () => {})} />);
  for (const text of ['Local evidence is offline', '300', 'receipts-nov-01-07', 'b7 · Sealed',
    'Nov 12, 09:42 UTC', 'Private evidence unavailable', 'Retry local evidence']) assert.ok(html.includes(text));
  assert.doesNotMatch(html, /Synthetic local receipt/);
});

it('distinguishes zero from no observation and never projects a private field into hosted metadata', () => {
  const local = { state: 'available' as const, record: 'Synthetic local receipt', description: 'Private note' };
  const html = renderToStaticMarkup(<OfflineEvidence hosted={{ ...retainedBenchmark, eligibleCases: 0,
    benchmark: null, observedAt: null }} local={local} />);
  assert.match(html, /ot-metric-value">0</);
  assert.match(html, /Hosted benchmark<\/dt><dd>Unavailable/);
  assert.match(html, /Observation time unavailable/);
  assert.match(html, /Private evidence<\/dt><dd>Synthetic local receipt/);
  const stale = { ...local, state: 'restricted' as const };
  const restricted = renderToStaticMarkup(<OfflineEvidence hosted={null} local={stale} />);
  assert.doesNotMatch(restricted, /Synthetic local receipt|Private note|Eligible cases|Hosted benchmark|Retry local evidence/);
});

it('omits an unauthorized hosted projection even when the local reader is offline', () => {
  const html = renderToStaticMarkup(<OfflineEvidence hosted={null} local={{ state: 'offline', onRetry: () => {} }} />);
  assert.match(html, /Retry local evidence/);
  assert.doesNotMatch(html, /Hosted metadata remains available|Eligible cases|Hosted benchmark/);
});
