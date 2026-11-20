import { useId } from 'react';
import { TableCell } from '../tokens/display';
import { chartFormat } from './chart-evidence';
import type { ChartAbsence, ObservedCount } from './chart-evidence.types';
import './paired-outcomes.css';

const outcomeLabels = {
  bothCorrect: 'Both correct',
  fixed: 'Fixed',
  regressed: 'Regressed',
  bothIncorrect: 'Both incorrect',
} as const;
type Outcome = keyof typeof outcomeLabels;
type Count = ObservedCount | ChartAbsence;

/** The caller supplies one authorized population. No value is recovered from another count. */
export interface PairedOutcomesProps {
  completePairs: number;
  expectedPairs: number;
  missingCandidate: Count;
  excluded: Count;
  unknown: Count;
  outcomes: Readonly<Record<Outcome, Count>>;
}

function exactValue(value: Count): string {
  if (value?.status === 'observed') return chartFormat.count(value.count);
  if ((value?.status !== 'missing' && value?.status !== 'unknown') ||
      typeof value.reason !== 'string' || !value.reason.trim()) {
    throw new Error('A paired count requires an observation or a named absence.');
  }
  return (value.status === 'missing' ? 'Missing' : 'Unknown') + ' · ' + value.reason;
}

export function PairedOutcomes({ completePairs, expectedPairs, missingCandidate, excluded, unknown, outcomes }: PairedOutcomesProps) {
  const id = useId();
  const complete = chartFormat.count(completePairs);
  const expected = chartFormat.count(expectedPairs);
  const missing = exactValue(missingCandidate);
  const rows = (Object.keys(outcomeLabels) as Outcome[]).map(key => {
    const value = outcomes[key];
    return { key, label: outcomeLabels[key], value, text: exactValue(value) };
  });
  const observedTotal = rows.reduce((total, { value }) => total + (value.status === 'observed' ? value.count : 0), 0);
  if (completePairs > expectedPairs ||
      (missingCandidate.status === 'observed' && missingCandidate.count > expectedPairs - completePairs) ||
      observedTotal > completePairs || (rows.every(({ value }) => value.status === 'observed') && observedTotal !== completePairs)) {
    throw new Error('The supplied pair counts disagree with the declared population.');
  }

  return <section className="ot-paired" aria-labelledby={id}>
    <h2 id={id} className="ot-chart-title">Paired outcomes</h2>
    <figure aria-labelledby={id} className="ot-paired-plot">
      <figcaption>Learning · {complete} complete pairs</figcaption>
      <ul>
        {rows.map(({ key, label, value, text }) => <li key={key} data-series={key}>
          <span>{label} · {text}</span>
          {value.status === 'observed' && value.count > 0 &&
            <div className="ot-paired-track" aria-hidden="true">
              <span className="ot-paired-mark" style={{ width: `${100 * value.count / completePairs}%` }} />
            </div>}
        </li>)}
      </ul>
      <div className="ot-paired-axis" aria-hidden="true" />
    </figure>
    <table>
      <caption>Paired outcomes · Exact values</caption>
      <thead><tr>
        <TableCell header="column" tone="blue" value="Measure" />
        <TableCell header="column" tone="blue" value="Exact value" />
      </tr></thead>
      <tbody>
        {rows.map(({ key, label, text }) => <tr key={key} data-series={key}>
          <TableCell header="row" value={label} /><TableCell value={text} />
        </tr>)}
        <tr><TableCell header="row" value="Excluded" /><TableCell value={exactValue(excluded)} /></tr>
        <tr><TableCell header="row" value="Unknown" /><TableCell value={exactValue(unknown)} /></tr>
      </tbody>
      <tfoot>
        <tr><TableCell header="row" value="Complete pairs" /><TableCell value={complete} /></tr>
        <tr><TableCell header="row" value="Missing candidate results" /><TableCell value={missing} /></tr>
        <tr><TableCell header="row" value="Expected pairs" /><TableCell value={expected} /></tr>
      </tfoot>
    </table>
  </section>;
}
