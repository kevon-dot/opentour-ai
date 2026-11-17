import type { MetricTone } from '../tokens/display.contracts';

type Reading =
  | { state: 'observed' | 'observed-zero'; value: string }
  | { state: 'missing' | 'unknown'; value?: never }
  | { state: 'zero-denominator'; denominator: '0'; value?: never };

/** All quantities are preformatted by the caller, including significant trailing zeroes. */
export type MetricSemanticsProps = Reading & {
  title: string;
  tone: MetricTone;
  unit: string;
  numerator: string;
  denominator: string;
  observationWindow: string;
  evidence: string;
  direction: 'Higher is better' | 'Lower is better' | 'No favorable direction';
  reason: string;
};
