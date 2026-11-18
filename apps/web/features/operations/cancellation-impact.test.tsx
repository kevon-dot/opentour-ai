import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
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
      expect(html).toContain(value);
    }
  });
  it('makes absent observations explicit without manufacturing a final value', () => {
    const html = renderToStaticMarkup(<CancellationImpact {...values} accruedLiability={null} reservation=" " />);
    expect(html.match(/>Unknown</g)).toHaveLength(2);
    expect(html).not.toContain('USD');
  });
  it('escapes labels, keeps row and column associations, and has no action controls', () => {
    const html = renderToStaticMarkup(<CancellationImpact {...values} />);
    expect(html).toContain('&lt;receipted copy&gt;');
    expect(html.match(/scope="col"/g)).toHaveLength(4);
    expect(html.match(/scope="row"/g)).toHaveLength(6);
    expect(html).not.toMatch(/<(?:button|input|a)\b/);
  });
});
