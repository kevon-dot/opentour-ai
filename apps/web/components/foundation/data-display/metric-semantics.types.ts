import type { MetricTone } from '../tokens/display.contracts';

type Reading =
  | { state: 'observed' | 'observed-zero' | 'forecast' | 'stale' | 'comparison' | 'incomplete' | 'gate' | 'summary'; value: string }
  | { state: 'missing' | 'unknown' | 'invalid' | 'incompatible'; value?: never }
  | { state: 'zero-denominator'; denominator: '0'; value?: never };

type ClearedReading = ({ state: 'revoked' } | { state: 'switching' }) & {
  title: string;
  tone: MetricTone;
  reason: string;
  value?: never;
  numerator?: never;
  denominator?: never;
  evidence?: never;
};

/** All quantities are preformatted by the caller, including significant trailing zeroes. */
export type MetricSemanticsProps = ClearedReading | (Reading & {
  title: string;
  tone: MetricTone;
  unit: string;
  numerator: string;
  denominator: string;
  observationWindow: string;
  evidence: string;
  direction: 'Higher is better' | 'Lower is better' | 'No favorable direction';
  reason: string;
});
