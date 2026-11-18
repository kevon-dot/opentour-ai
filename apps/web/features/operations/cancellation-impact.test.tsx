import { renderToStaticMarkup } from 'react-dom/server';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { CancellationImpact, type CancellationImpactProps } from './cancellation-impact';

const values: CancellationImpactProps = {
  accruedLiability: 'USD 0.00 accrued · not final', reservation: 'USD 00.50 maximum',
  retainedArtifact: 'Retained', lastHeartbeat: 'Observation A', heartbeatAge: 'Age at observation',
  effects: [{ id: 'sample', label: '<receipted copy>', disposition: 'Irrevocable copy' }],
};
describe('read-only cancellation observations', () => {
  it('preserves the supplied financial and observation strings', () => {
    const html = renderToStaticMarkup(<CancellationImpact {...values} />);
    for (const value of [values.accruedLiability!, values.reservation!, values.lastHeartbeat!, values.heartbeatAge!]) {
      assert.ok((html)?.includes(value));
    }
  });
  it('makes absent observations explicit without manufacturing a final value', () => {
    const html = renderToStaticMarkup(<CancellationImpact {...values} accruedLiability={null} reservation=" " />);
    assert.equal((html.match(/>Unknown</g))?.length, 2);
    assert.ok((html)?.includes('USD') === false);
  });
  it('escapes labels, keeps row and column associations, and has no action controls', () => {
    const html = renderToStaticMarkup(<CancellationImpact {...values} />);
    assert.ok((html)?.includes('&lt;receipted copy&gt;'));
    assert.equal((html.match(/scope="col"/g))?.length, 4);
    assert.equal((html.match(/scope="row"/g))?.length, 6);
    assert.doesNotMatch(html, /<(?:button|input|a)\b/);
  });
});
