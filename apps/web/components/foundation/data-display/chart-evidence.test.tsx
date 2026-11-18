import { renderToStaticMarkup as render } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ChartEvidenceNotice, EvidenceCoverage, RateComparison, SignedBars } from './chart-evidence';
import type { EvidenceCoverageProps, EvidenceStatementTone, RateComparisonProps, SignedBarsProps } from './chart-evidence.types';

const coverage: EvidenceCoverageProps = {
  title: 'Evidence coverage', denominator: 1600, note: 'Sample window · Nov 1–7 · observed 09:42 UTC',
  segments: [
    { key: 'verified', label: 'Verified', series: 'mint', value: { status: 'observed', count: 1284 } },
    { key: 'awaiting', label: 'Awaiting evidence', series: 'amber', value: { status: 'observed', count: 196 } },
    { key: 'excluded', label: 'Excluded', series: 'coral', value: { status: 'observed', count: 120 } },
  ],
};
const rate: RateComparisonProps = {
  title: 'Material errors fell by half', subtitle: 'Same 200 episodes · lower is better · simulated fixture A',
  rows: [
    { key: 'repair', label: 'Best locked repair', series: 'lilac', value: { status: 'observed', numerator: 60, denominator: 200, rate: 0.3 } },
    { key: 'r1', label: 'Candidate r1', series: 'mint', value: { status: 'observed', numerator: 30, denominator: 200, rate: 0.15 } },
  ], rateFractionDigits: 0,
  method: 'Paired episodes · frozen two-sided 95% exact-marginal interval with Bonferroni coverage. Retention uses a frozen one-sided bound. No method switch after observing results.',
};
const signed: SignedBarsProps = {
  label: 'Signed amounts', unit: { currency: 'USD', fractionDigits: 0 }, domain: { min: -900, max: 1800 },
  rows: [-900, 300, 1800].map(value => ({ key: String(value), value: { status: 'observed', value } })),
};

describe('approved evidence chart forms', () => {
  it('includes an equivalent definition list for every coverage segment', () => {
    const html = render(<EvidenceCoverage {...coverage} />);
    for (const text of ['Verified', '1,284 / 1,600', 'Awaiting evidence', '196 / 1,600', 'Excluded', '120 / 1,600', coverage.note]) expect(html).toContain(text);
    expect(html.match(/<dt /g)).toHaveLength(3);
  });
  it('draws supplied rates on the same scale and keeps the method visible', () => {
    const html = render(<RateComparison {...rate} />);
    for (const text of ['60 / 200 · 30%', '30 / 200 · 15%', rate.method, '0%', '50%', '100%']) expect(html).toContain(text);
    expect(html.match(/<li /g)).toHaveLength(2);
  });
  it('keeps signed dollar values available as text', () => {
    const html = render(<SignedBars {...signed} />);
    for (const text of ['−$900', '$300', '$1,800']) expect(html).toContain(text);
    expect(html.match(/<li /g)).toHaveLength(3);
  });
  it('keeps observed zero distinct from a missing or unknown count', () => {
    const html = render(<EvidenceCoverage {...coverage} segments={[
      { ...coverage.segments[0], value: { status: 'observed', count: 0 } },
      { ...coverage.segments[1], value: { status: 'missing', reason: 'Source has not reported' } },
      { ...coverage.segments[2], value: { status: 'unknown', reason: 'Observation unavailable' } },
    ]} />);
    for (const text of ['0 / 1,600', 'Source has not reported', 'Observation unavailable']) expect(html).toContain(text);
    expect(html).not.toContain('class="ot-chart-coverage-segment');
  });
  it('rejects impossible counts and marks outside the declared scale', () => {
    expect(() => render(<EvidenceCoverage {...coverage} denominator={1} />)).toThrow();
    expect(() => render(<SignedBars {...signed} domain={{ min: -10, max: 10 }} />)).toThrow();
  });
  const notices: [EvidenceStatementTone, string, string][] = [
    ['success', '0 / 300', '0.0% observed · 95% upper bound 0.994% · Limit 1.000% · Passed'],
    ['hold', '0 / 240', '95% upper bound 1.240% · Limit 1.000% · Inconclusive'],
    ['info', '60 / 200', '30 / 200 · 15 pp · 50% relative error reduction'],
    ['hold', '180 of 200 pairs scored', '20 candidate results missing · Incomplete'],
    ['info', 'A 50', 'B 30 · Other 20 · View all categories'],
    ['hold', '400 cases', '−0.5 pp · Lower bound −3.98 pp · Required lower bound greater than −1.00 pp · Inconclusive'],
  ];
  it.each(notices)('preserves %s notice %s and exact supplied bounds', (tone, headline, body) => {
    const html = render(<ChartEvidenceNotice tone={tone} headline={headline} details={[body]} />);
    expect(html).toContain(`ot-notice--${tone}`);
    expect(html).toContain(headline);
    expect(html).toContain(body);
  });
});
