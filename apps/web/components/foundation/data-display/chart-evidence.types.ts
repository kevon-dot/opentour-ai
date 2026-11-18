/**
 * Presentation inputs only. Every number is drawn and printed exactly as given: these components
 * compute no rate, interval, total or decision, and no prop represents server authority.
 * The contracts are closed: a value outside them is refused, never coerced.
 */

/** The four chart fills of the design (`series/*`). Colour never carries meaning alone: every mark has text. */
export type ChartSeries = 'mint' | 'lilac' | 'amber' | 'coral';

/** Heading element used for a chart title; the visual style does not change with the level. */
export type ChartHeadingLevel = 2 | 3 | 4;

/**
 * An explicit absence. It is drawn as a gap and printed as its reason. It is never drawn or
 * printed as zero, and it never contributes a length to a bar.
 */
export interface ChartAbsence {
  status: 'missing' | 'unknown';
  /** Visible text shown where the value would be. */
  reason: string;
}

/** A count that was observed. Zero is a valid observation. */
export interface ObservedCount {
  status: 'observed';
  /** Non-negative whole number. */
  count: number;
}

export interface CoverageSegment {
  /** Unique within the chart. */
  key: string;
  /** Visible name of the segment in the key. */
  label: string;
  series: ChartSeries;
  value: ObservedCount | ChartAbsence;
}

/** Segmented coverage bar with a key that lists every segment's count and the shared denominator. */
export interface EvidenceCoverageProps {
  title: string;
  /** Defaults to 2. */
  headingLevel?: ChartHeadingLevel;
  /** Size of the whole population. Printed after every count; the full bar length represents it. */
  denominator: number;
  /** In drawing order. Observed counts may not add up to more than the denominator. */
  segments: readonly CoverageSegment[];
  /** Visible provenance line under the key, for example the sample window and observation time. */
  note: string;
}

/** A rate that was observed, with the count and denominator it is reported with. */
export interface ObservedRate {
  status: 'observed';
  /** Non-negative whole number, at most the denominator. */
  numerator: number;
  /** Positive whole number. */
  denominator: number;
  /** The rate to draw and print, as a fraction from 0 to 1. It is taken as given, not derived. */
  rate: number;
}

export interface RateComparisonRow {
  /** Unique within the chart. */
  key: string;
  /** Visible name of the compared series. */
  label: string;
  series: ChartSeries;
  value: ObservedRate | ChartAbsence;
}

/** Horizontal rate bars on a fixed scale from 0% to 100%. */
export interface RateComparisonProps {
  title: string;
  /** Defaults to 2. */
  headingLevel?: ChartHeadingLevel;
  /** Visible population and reading direction, for example the shared episodes and "lower is better". */
  subtitle: string;
  /** Decimal places of the printed percentage (0 to 20). Changes the text only, never the bar. */
  rateFractionDigits: number;
  rows: readonly RateComparisonRow[];
  /** Visible method label, including how uncertainty was obtained. */
  method: string;
}

/** A signed amount that was observed. Negative values and zero are valid observations. */
export interface ObservedAmount {
  status: 'observed';
  value: number;
}

export interface SignedBarRow {
  /** Unique within the chart. */
  key: string;
  value: ObservedAmount | ChartAbsence;
}

/** Unit of a signed-bar chart. US dollars is the only unit the design shows. */
export interface SignedBarUnit {
  currency: 'USD';
  /** Decimal places of the printed amounts (0 to 20). Changes the text only, never the bars. */
  fractionDigits: number;
}

/** Bars drawn left (negative) and right (positive) of a zero line, with a zero-aligned axis. */
export interface SignedBarsProps {
  /** Accessible name of the value list. The design shows no visible title for this form. */
  label: string;
  unit: SignedBarUnit;
  /** Axis limits. `min` is below zero, `max` above, and every observed value lies inside. */
  domain: { min: number; max: number };
  rows: readonly SignedBarRow[];
}

/** Tones of the text statements, in the design's Notice vocabulary. */
export type EvidenceStatementTone = 'info' | 'success' | 'hold' | 'failure' | 'unknown';

/**
 * Evidence the design shows as text only: bounds, limits, paired counts and exclusions.
 * The strings are printed as given, in order, and are never parsed.
 */
export interface EvidenceStatement {
  tone: EvidenceStatementTone;
  /** First line, for example a count and its denominator. */
  headline: string;
  /** Remaining visible statements in reading order (at least one). */
  details: readonly string[];
}

/** The two visible lines of a text statement. */
export interface EvidenceStatementText {
  tone: EvidenceStatementTone;
  title: string;
  body: string;
}
