import type { CSSProperties } from 'react';
import { Metric } from '../tokens/display';
import { requireLabel } from '../tokens/require-label';
import type { MetricSemanticsProps } from './metric-semantics.types';

const labels = {
  observed: 'Observed',
  'observed-zero': 'Observed zero',
  missing: 'Not available',
  'zero-denominator': 'No eligible cases',
  unknown: 'Unknown',
} as const;

const metadata: CSSProperties = {
  margin: 'var(--ot-space-8) 0 0',
  fontFamily: 'Geist, sans-serif',
  fontSize: 'var(--ot-type-small-size)',
  lineHeight: 'var(--ot-type-small-line)',
  color: 'var(--ot-text-secondary)',
  overflowWrap: 'anywhere',
};

export function MetricSemantics(props: MetricSemanticsProps) {
  const { state, title, tone, direction, unit, numerator, denominator, observationWindow, evidence, reason } = props;
  for (const [name, content] of Object.entries({ unit, numerator, denominator, observationWindow, evidence, reason, direction })) {
    requireLabel(content, name);
  }
  let value: string = labels[state];
  if (state === 'observed' || state === 'observed-zero') {
    requireLabel(props.value, 'metric reading');
    value = props.value;
  }

  return <div style={{ WebkitFontSmoothing: 'antialiased' }}>
    <Metric tone={tone} title={title} value={value} detail={reason} />
    <div style={metadata}>
      <div>{labels[state]} · {direction}</div>
      <div>Unit: {unit}</div>
      <div>Numerator: {numerator} · Denominator: {denominator}</div>
      <div>Window: {observationWindow}</div>
      <div>Evidence: {evidence}</div>
    </div>
  </div>;
}
