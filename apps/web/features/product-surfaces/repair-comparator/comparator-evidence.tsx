import { EvidenceRecords } from '../../../components/foundation/data-display/evidence-records';
import { Metric } from '../../../components/foundation/tokens/display';
import { Notice } from '../../../components/foundation/tokens/notice';
import './comparator-evidence.css';

/** Observations retain caller precision, including trailing zeroes, and explain missing values. */
export type ComparatorReading = { value: string; unavailable?: never } | { value?: never; unavailable: string };
export type ComparatorAlternative = {
  id: string;
  name: string;
  errors: ComparatorReading;
  denominator: ComparatorReading;
  exclusions: ComparatorReading;
  effort: string;
  costPerCase: ComparatorReading;
  latencyP95: ComparatorReading;
  samePopulation: 'Yes' | 'No' | 'Unknown';
  sameEvidence: 'Yes' | 'No' | 'Unknown';
};
export type ComparatorEvidenceProps = {
  frozenIdentity: string;
  selectionPolicy: string;
  tieHandling: string;
  quality: { kind: 'tied'; explanation: string } | { kind: 'reported'; title: string; explanation: string };
  coverage: { state: 'Complete' | 'Partial' | 'Unknown'; detail: string };
  parity: { state: 'Matched' | 'Mismatched (blocking)' | 'Unknown'; reason: string };
  alternatives: readonly ComparatorAlternative[];
  recommendation: { result: 'Train' | 'Do not train' | 'Defer'; reason: string; nextStep: string };
};

function reading(metric: ComparatorReading, suffix = '') {
  return metric.value === undefined ? `Unavailable — ${metric.unavailable}` : metric.value + suffix;
}

export function ComparatorEvidence(props: ComparatorEvidenceProps) {
  const { quality, coverage, recommendation, parity, alternatives } = props;
  return <div className="ot-comparison">
    <Notice tone="info" title={quality.kind === 'tied' ? 'Quality results are tied' : quality.title} body={quality.explanation} />
    <div className="ot-comparison-metrics">
      <Metric tone="mint" title="Comparator" value="Frozen" detail={props.frozenIdentity} />
      <Metric tone="lilac" title="Recommendation" value={recommendation.result} detail={recommendation.reason} />
      <Metric tone="blue" title="Evidence" value={coverage.state} detail={coverage.detail} />
    </div>
    <div className="ot-comparison-section">
      <h3>Frozen selection policy</h3>
      <p>{props.selectionPolicy}</p><p>{props.tieHandling}</p>
    </div>
    <EvidenceRecords title="Current evidence" columns={['Alternative', 'Errors', 'Cost / case', 'Latency p95', 'Exclusions']}
      rows={alternatives.map(a => ({ key: a.id, cells: [a.name, `${reading(a.errors)} / ${reading(a.denominator)}`,
        reading(a.costPerCase), reading(a.latencyP95), reading(a.exclusions)] }))} />
    <Notice tone={parity.state === 'Matched' ? 'info' : parity.state === 'Unknown' ? 'unknown' : 'hold'}
      title={parity.state} body={parity.reason} />
    <EvidenceRecords title="Evidence parity" columns={['Alternative', 'Same population', 'Same evidence', 'Exclusions']}
      rows={alternatives.map(a => ({ key: a.id, cells: [a.name, a.samePopulation, a.sameEvidence, reading(a.exclusions)] }))} />
    <EvidenceRecords title="Alternative effort" columns={['Alternative', 'Effort', 'Cost', 'Latency']}
      rows={alternatives.map(a => ({ key: a.id, cells: [a.name, a.effort, reading(a.costPerCase, ' / case'),
        reading(a.latencyP95, ' p95')] }))} />
    <EvidenceRecords title="Training recommendation" columns={['Result', 'Meaning', 'Next step']}
      rows={[{ key: 'result', cells: [recommendation.result, recommendation.reason, recommendation.nextStep] }]} />
  </div>;
}
