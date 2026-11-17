import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { MetricSemantics } from './metric-semantics';
import type { MetricSemanticsProps } from './metric-semantics.types';

const base = {
  title: 'Critical error rate', tone: 'blue', unit: '%', numerator: '0', denominator: '300',
  observationWindow: 'Nov 1–7', evidence: 'Observed', direction: 'Lower is better',
} as const;

describe('metric semantics', () => {
  const cases = [
    ['0.0000%', { state: 'observed-zero', value: '0.0000%', reason: '0 errors / 300 cases' }],
    ['Not available', { state: 'missing', reason: 'Source has not reported a count' }],
    ['No eligible cases', { state: 'zero-denominator', denominator: '0', reason: '0 eligible cases · Rate is not defined' }],
    ['Unknown', { state: 'unknown', reason: 'Observation status has not been confirmed.' }],
    ['0.994%', { state: 'observed', value: '0.994%', reason: 'Supplied upper bound' }],
  ] as const;
  it.each(cases)('renders %s with exact quantities and context', (expected, reading) => {
    const props: MetricSemanticsProps = { ...base, ...reading };
    const html = renderToStaticMarkup(<MetricSemantics {...props} />);
    expect(html).toContain(`<dd class="ot-metric-value">${expected}</dd>`);
    for (const text of [props.reason, base.direction, 'Unit: %', 'Numerator: 0',
      'Denominator: ' + props.denominator, 'Window: Nov 1–7', 'Evidence: Observed']) {
      expect(html).toContain(text);
    }
    expect(html).not.toContain('aria-hidden');
  });
  it.each([
    ['revoked', 'Access revoked'],
    ['switching', 'Switching…'],
  ] as const)('clears all supplied measurement details when %s', (state, text) => {
    const props = { ...base, state, value: 'PRIVATE-OLD-VALUE', evidence: 'PRIVATE-OLD-EVIDENCE',
      numerator: 'PRIVATE-OLD-COUNT', title: 'Private metric', reason: 'Previous data is hidden.' };
    const html = renderToStaticMarkup(<MetricSemantics {...props as unknown as MetricSemanticsProps} />);
    expect(html).toContain(text);
    expect(html).toContain(props.reason);
    expect(html).not.toMatch(/PRIVATE-OLD|Numerator:|Evidence:/);
  });
  it.each([
    ['invalid', 'Result unavailable'],
    ['incompatible', 'Comparison unavailable'],
  ] as const)('suppresses an old result when the new state is %s', (state, text) => {
    const props = { ...base, state, reason: 'Supplied comparison is undefined.' };
    const extra = { value: 'PRIVATE-OLD-DELTA' };
    const html = renderToStaticMarkup(<MetricSemantics {...{ ...props, ...extra } as unknown as MetricSemanticsProps} />);
    expect(html).toContain(text);
    expect(html).not.toContain(extra.value);
  });
  it('refuses blank readings and context', () => {
    const props = { ...base, state: 'observed', value: '1.000%', reason: 'Confirmed reading' } as const;
    for (const key of ['value','reason','unit','numerator','denominator','observationWindow','evidence'] as const) {
      expect(() => renderToStaticMarkup(<MetricSemantics {...props} {...{ [key]: ' ' }} />)).toThrow();
    }
  });
});
