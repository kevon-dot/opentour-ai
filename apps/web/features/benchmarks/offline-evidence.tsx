import { Button } from '../../components/foundation/tokens';
import { Metric } from '../../components/foundation/tokens/display';
import { Notice } from '../../components/foundation/tokens/notice';
import './offline-evidence.css';

export interface HostedBenchmark {
  benchmark: string | null;
  observedAt: string | null;
  eligibleCases: number | null;
  population: string | null;
}

export type LocalEvidence =
  | { state: 'available'; record: string; description?: string }
  | { state: 'offline'; onRetry: () => void }
  | { state: 'restricted' };

export interface OfflineEvidenceProps {
  /** Null means the hosted projection is not authorized for this view. */
  hosted: HostedBenchmark | null;
  local: LocalEvidence;
}

export function OfflineEvidence({ hosted, local }: OfflineEvidenceProps) {
  return <section className="ot-offline-evidence" aria-label="Benchmark evidence">
    {local.state === 'offline' && <Notice tone="unknown" title="Local evidence is offline"
      body={hosted ? 'The customer environment cannot be reached. Hosted metadata remains available.' :
        'The customer environment cannot be reached.'} />}
    {hosted && <>
      <Metric title="Eligible cases" tone="blue" value={hosted.eligibleCases?.toString() ?? 'Unavailable'}
        detail={hosted.population ?? 'Population unavailable'} />
      <dl className="ot-benchmark-field">
        <dt>Hosted benchmark</dt><dd>{hosted.benchmark ?? 'Unavailable'}</dd>
        <dd className="ot-observation">{hosted.observedAt ? 'Hosted metadata last observed ' + hosted.observedAt : 'Observation time unavailable'}</dd>
      </dl>
    </>}
    {local.state === 'available' && <dl className="ot-benchmark-field">
      <dt>Private evidence</dt><dd>{local.record}</dd>
      {local.description && <dd className="ot-observation">{local.description}</dd>}
    </dl>}
    {local.state === 'offline' && <>
      <Notice tone="unknown" title="Private evidence unavailable"
        body="Local records are not available while the customer environment is offline." />
      <Button variant="secondary" label="Retry local evidence" onPress={local.onRetry} />
    </>}
    {local.state === 'restricted' && <Notice tone="unknown" title="Access unavailable"
      body="Private evidence is unavailable for your current access." />}
  </section>;
}
