import type { CSSProperties, ReactNode } from 'react';
import { Metric } from '../tokens/display';
import { requireLabel } from '../tokens/require-label';
import type { MetricSemanticsProps } from './metric-semantics.types';

const labels = {
  observed: 'Observed',
  'observed-zero': 'Observed zero',
  missing: 'Not available',
  'zero-denominator': 'No eligible cases',
  unknown: 'Unknown',
  forecast: 'Forecast',
  stale: 'Stale',
  comparison: 'Comparison',
  incomplete: 'Incomplete',
  gate: 'Gate result',
  summary: 'Release summary',
  invalid: 'Result unavailable',
  incompatible: 'Comparison unavailable',
  revoked: 'Access revoked',
  switching: 'Switching…',
} as const;

const metadata: CSSProperties = {
  margin: 'var(--ot-space-8) 0 0',
  fontFamily: 'Geist, sans-serif',
  fontSize: 'var(--ot-type-small-size)',
  lineHeight: 'var(--ot-type-small-line)',
  color: 'var(--ot-text-secondary)',
  overflowWrap: 'anywhere',
};

// Container queries respond to the space available to this component at the user's text size.
const narrowStyles = `
  @container metric-semantics (max-width: 11em) {
    .ot-metric-semantics[data-state='unknown'] > .ot-metric {
      padding-inline: var(--ot-space-8);
    }
    .ot-metric-semantics:is([data-state='summary'], [data-state='switching']) .ot-metric-value {
      font-size: var(--ot-type-heading-size);
      line-height: var(--ot-type-heading-line);
    }
  }
`;

function MetricLayout({ state, children }: { state: MetricSemanticsProps['state']; children: ReactNode }) {
  return <div className="ot-metric-semantics" data-state={state}
    style={{ WebkitFontSmoothing: 'antialiased', container: 'metric-semantics / inline-size' }}>
    <style href="ot-metric-semantics" precedence="default">{narrowStyles}</style>
    {children}
  </div>;
}

export function MetricSemantics(props: MetricSemanticsProps) {
  if (props.state === 'revoked' || props.state === 'switching') {
    return <MetricLayout state={props.state}>
      <Metric title={props.title} tone={props.tone} value={labels[props.state]} detail={props.reason} />
    </MetricLayout>;
  }
  const { state, title, tone, direction, unit, numerator, denominator, observationWindow, evidence, reason } = props;
  for (const [name, content] of Object.entries({ unit, numerator, denominator, observationWindow, evidence, reason, direction })) {
    requireLabel(content, name);
  }
  let value: string = labels[state];
  switch (state) {
  case 'observed':
  case 'observed-zero':
  case 'forecast':
  case 'stale':
  case 'comparison':
  case 'incomplete':
  case 'gate':
  case 'summary':
    requireLabel(props.value, 'metric reading');
    value = props.value;
    break;
  }

  return <MetricLayout state={state}>
    <Metric tone={tone} title={title} value={value} detail={reason} />
    <div style={metadata}>
      <div>{labels[state]} · {direction}</div>
      <div>Unit: {unit}</div>
      <div>Numerator: {numerator} · Denominator: {denominator}</div>
      <div>Window: {observationWindow}</div>
      <div>Evidence: {evidence}</div>
    </div>
  </MetricLayout>;
}
