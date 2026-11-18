import { useId } from 'react';
import { Notice } from '../tokens/notice';
import type { CSSProperties } from 'react';
import type { ChartAbsence, ChartHeadingLevel, ChartSeries, EvidenceCoverageProps, EvidenceStatement,
  EvidenceStatementText, EvidenceStatementTone, RateComparisonProps, SignedBarsProps } from './chart-evidence.types';
import '../../../styles/product-tokens.css';
import './chart-evidence.css';

export type { ChartAbsence, ChartHeadingLevel, ChartSeries, CoverageSegment, EvidenceCoverageProps, EvidenceStatement,
  EvidenceStatementText, EvidenceStatementTone, ObservedAmount, ObservedCount, ObservedRate, RateComparisonProps,
  RateComparisonRow, SignedBarRow, SignedBarUnit, SignedBarsProps } from './chart-evidence.types';

/** The design's chart fills (`series/*`), in the order of styles/product-tokens.css. */
export const chartSeries: readonly ChartSeries[] = ['mint', 'lilac', 'amber', 'coral'];
/** Statement tones, in the Figma variant order of the Notice component set 12:17. */
export const evidenceStatementTones: readonly EvidenceStatementTone[] = ['info', 'success', 'hold', 'failure', 'unknown'];
/** Separator between the details of a text statement, as the design prints it. */
export const statementSeparator = ' · ';

const headingLevels: readonly ChartHeadingLevel[] = [2, 3, 4];

type ChartProperty = '--ot-chart-count' | '--ot-chart-total' | '--ot-chart-gaps' | '--ot-chart-rate'
  | '--ot-chart-value' | '--ot-chart-min' | '--ot-chart-max';
/** Hands numbers to the stylesheet unchanged; every bar length is computed there, not here. */
const chartNumbers = (numbers: Partial<Record<ChartProperty, number>>) => numbers as CSSProperties;

function refuse(message: string): never { throw new Error(message); }

/** Visible text is a contract: blank or non-string text is refused, never rendered empty. */
function requireText(text: unknown, name: string): asserts text is string {
  if (typeof text !== 'string' || text.trim().length === 0) refuse(`A visible chart ${name} is required.`);
}
function requireCount(value: unknown, name: string, minimum: 0 | 1): asserts value is number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < minimum) {
    refuse(`A chart ${name} is a whole number of at least ${minimum}.`);
  }
}
function requireFinite(value: unknown, name: string): asserts value is number {
  if (typeof value !== 'number' || !Number.isFinite(value)) refuse(`A chart ${name} is a finite number.`);
}
function requireFractionDigits(value: unknown): asserts value is number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0 || value > 20) {
    refuse('Chart fraction digits are a whole number from 0 to 20.');
  }
}
function requireRows<Row extends { key: string }>(rows: readonly Row[], name: string): void {
  if (!Array.isArray(rows) || rows.length === 0) refuse(`A chart needs at least one ${name}.`);
  for (const row of rows) requireText(row?.key, `${name} key`);
  if (new Set(rows.map(row => row.key)).size !== rows.length) refuse(`Chart ${name} keys are unique.`);
}
function requireSeries(series: unknown): asserts series is ChartSeries {
  if (!chartSeries.some(known => known === series)) refuse('A declared chart series is required.');
}
/** Anything that is not an observation must be a declared absence with a reason; nothing is coerced to zero. */
function requireAbsence(value: { status: string }): asserts value is ChartAbsence {
  if (value?.status !== 'missing' && value?.status !== 'unknown') refuse('A chart value is observed, missing or unknown.');
  requireText((value as ChartAbsence).reason, 'absence reason');
}

/** The design prints negative numbers with U+2212 MINUS SIGN; Intl emits U+002D. */
const typographicMinus = (text: string) => text.replaceAll('-', '\u2212');
const fixedDigits = (fractionDigits: number) => {
  requireFractionDigits(fractionDigits);
  return { minimumFractionDigits: fractionDigits, maximumFractionDigits: fractionDigits };
};

/**
 * The one way these charts print a number, exported so text placed beside a chart reads the same.
 * Formatting rounds the printed text to the requested digits and keeps the sign; it never changes
 * the number a bar is drawn from.
 */
export const chartFormat = {
  /** `1284` -> `1,284`. */
  count(value: number): string {
    requireCount(value, 'count', 0);
    return new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(value);
  },
  /** A fraction as a percentage: `0.00993608` with 3 digits -> `0.994%`. */
  percent(fraction: number, fractionDigits: number): string {
    requireFinite(fraction, 'percentage');
    return typographicMinus(new Intl.NumberFormat('en-US', { style: 'percent', ...fixedDigits(fractionDigits) }).format(fraction));
  },
  /** Percentage points: `-3.97651` with 2 digits -> `−3.98 pp`. */
  percentagePoints(value: number, fractionDigits: number): string {
    requireFinite(value, 'percentage-point value');
    return `${typographicMinus(new Intl.NumberFormat('en-US', fixedDigits(fractionDigits)).format(value))} pp`;
  },
  /** US dollars: `-900` with 0 digits -> `−$900`. */
  usd(value: number, fractionDigits: number): string {
    requireFinite(value, 'amount');
    return typographicMinus(new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', ...fixedDigits(fractionDigits) }).format(value));
  },
} as const;

function Title({ id, level, text }: { id: string; level: ChartHeadingLevel; text: string }) {
  if (!headingLevels.includes(level)) refuse('A chart heading level is 2, 3 or 4.');
  const Heading = `h${level}` as const;
  return <Heading id={id} className="ot-chart-title">{text}</Heading>;
}

/**
 * Segmented coverage bar (Figma 5:176). The key below the bar is the equivalent list: it prints every
 * segment's count with the shared denominator, so the bar itself is hidden from assistive technology.
 * An absent or zero count draws no segment; the bar length it would have used stays empty.
 */
export function EvidenceCoverage({ title, headingLevel = 2, denominator, segments, note }: EvidenceCoverageProps) {
  const titleId = useId();
  requireText(title, 'title');
  requireText(note, 'note');
  requireCount(denominator, 'denominator', 1);
  requireRows(segments, 'segment');
  let accounted = 0;
  for (const { label, series, value } of segments) {
    requireText(label, 'segment label');
    requireSeries(series);
    if (value?.status === 'observed') { requireCount(value.count, 'segment count', 0); accounted += value.count; }
    else requireAbsence(value);
  }
  if (accounted > denominator) refuse('Coverage counts exceed the denominator; the bar cannot represent them.');
  const total = chartFormat.count(denominator);
  const drawn = segments.flatMap(({ key, series, value }) =>
    value.status === 'observed' && value.count > 0 ? [{ key, series, count: value.count }] : []);
  return <figure className="ot-chart ot-chart--card ot-chart-coverage" aria-labelledby={titleId}>
    <Title id={titleId} level={headingLevel} text={title} />
    <div className="ot-chart-coverage-bar" aria-hidden="true"
      style={chartNumbers({ '--ot-chart-total': denominator, '--ot-chart-gaps': Math.max(drawn.length - 1, 0) })}>
      {drawn.map(({ key, series, count }) => <span key={key}
        className={`ot-chart-coverage-segment ot-chart-series--${series}`} style={chartNumbers({ '--ot-chart-count': count })} />)}
    </div>
    <dl className="ot-chart-coverage-key">
      {segments.map(({ key, label, value }) => <div key={key} className="ot-chart-coverage-item" data-status={value.status}>
        <dt className="ot-chart-coverage-label">{label}</dt>
        <dd className="ot-chart-coverage-value">
          {value.status === 'observed' ? `${chartFormat.count(value.count)} / ${total}` : value.reason}
        </dd>
      </div>)}
    </dl>
    <p className="ot-chart-note">{note}</p>
  </figure>;
}

/**
 * Rate bars on a fixed 0-100% scale (Figma 5:280). The rows are an ordered list whose visible text
 * (label, count / denominator, rate) is the equivalent of the bars, so tracks and axis are hidden from
 * assistive technology. An observed zero keeps its empty full-scale track and prints its numbers; an
 * absent value has no track and prints its reason instead of a number.
 */
export function RateComparison({ title, headingLevel = 2, subtitle, rateFractionDigits, rows, method }: RateComparisonProps) {
  const titleId = useId();
  requireText(title, 'title');
  requireText(subtitle, 'subtitle');
  requireText(method, 'method label');
  requireFractionDigits(rateFractionDigits);
  requireRows(rows, 'row');
  for (const { label, series, value } of rows) {
    requireText(label, 'row label');
    requireSeries(series);
    if (value?.status === 'observed') {
      requireCount(value.denominator, 'rate denominator', 1);
      requireCount(value.numerator, 'rate numerator', 0);
      requireFinite(value.rate, 'rate');
      if (value.numerator > value.denominator) refuse('A rate numerator is at most its denominator.');
      if (value.rate < 0 || value.rate > 1) refuse('A rate lies on the fixed scale from 0 to 1; it is never clipped.');
    } else requireAbsence(value);
  }
  return <figure className="ot-chart ot-chart--card ot-chart-rate" aria-labelledby={titleId}>
    <Title id={titleId} level={headingLevel} text={title} />
    <p className="ot-chart-subtitle">{subtitle}</p>
    <ol className="ot-chart-rate-rows">
      {rows.map(({ key, label, series, value }) => <li key={key} className="ot-chart-rate-row" data-status={value.status}>
        <div className="ot-chart-rate-head">
          <span className="ot-chart-rate-label">{label}</span>
          <span className="ot-chart-rate-value">{value.status === 'observed'
            ? `${chartFormat.count(value.numerator)} / ${chartFormat.count(value.denominator)}${statementSeparator}${chartFormat.percent(value.rate, rateFractionDigits)}`
            : value.reason}</span>
        </div>
        {value.status === 'observed' && <div className="ot-chart-rate-track" aria-hidden="true">
          {value.rate > 0 && <span className={`ot-chart-rate-fill ot-chart-series--${series}`}
            style={chartNumbers({ '--ot-chart-rate': value.rate })} />}
        </div>}
      </li>)}
    </ol>
    <div className="ot-chart-rate-axis" aria-hidden="true">
      <span className="ot-chart-rate-tick ot-chart-rate-tick--zero">{chartFormat.percent(0, 0)}</span>
      <span className="ot-chart-rate-tick ot-chart-rate-tick--middle">{chartFormat.percent(0.5, 0)}</span>
      <span className="ot-chart-rate-tick ot-chart-rate-tick--max">{chartFormat.percent(1, 0)}</span>
    </div>
    <p className="ot-chart-method">{method}</p>
  </figure>;
}

/**
 * Signed bars around a zero line (Figma 24:148-24:156) with the zero-aligned axis 56:129. Negative
 * values run left of zero and positive values right. The design prints no value beside a bar, so each
 * list item carries its formatted amount as text for assistive technology; the drawing and the axis are
 * hidden from it. An absent value draws no bar and no track and prints its reason. A value outside the
 * axis is refused, never clipped.
 */
export function SignedBars({ label, unit, domain, rows }: SignedBarsProps) {
  requireText(label, 'label');
  if (unit?.currency !== 'USD') refuse('Signed bars are drawn in US dollars, the only unit the design shows.');
  requireFractionDigits(unit.fractionDigits);
  requireFinite(domain?.min, 'axis minimum');
  requireFinite(domain.max, 'axis maximum');
  if (!(domain.min < 0 && domain.max > 0)) refuse('A signed axis has its minimum below zero and its maximum above zero.');
  requireRows(rows, 'row');
  for (const { value } of rows) {
    if (value?.status === 'observed') {
      requireFinite(value.value, 'value');
      if (value.value < domain.min || value.value > domain.max) refuse('A chart value lies inside the axis; it is never clipped.');
    } else requireAbsence(value);
  }
  const amount = (value: number) => chartFormat.usd(value, unit.fractionDigits);
  return <div className="ot-chart ot-chart-signed"
    style={chartNumbers({ '--ot-chart-min': domain.min, '--ot-chart-max': domain.max })}>
    <ol className="ot-chart-signed-rows" aria-label={label}>
      {rows.map(({ key, value }) => value.status === 'observed'
        ? <li key={key} className="ot-chart-signed-item" data-status="observed">
          <span className="ot-chart-visually-hidden">{amount(value.value)}</span>
          <div className="ot-chart-signed-row" aria-hidden="true">
            <div className="ot-chart-signed-half ot-chart-signed-half--negative">
              {value.value < 0 && <span className="ot-chart-signed-bar ot-chart-signed-bar--negative"
                style={chartNumbers({ '--ot-chart-value': value.value })} />}
            </div>
            <div className="ot-chart-signed-half ot-chart-signed-half--positive">
              {value.value > 0 && <span className="ot-chart-signed-bar ot-chart-signed-bar--positive"
                style={chartNumbers({ '--ot-chart-value': value.value })} />}
            </div>
            <span className="ot-chart-signed-zero" />
          </div>
        </li>
        : <li key={key} className="ot-chart-signed-item" data-status={value.status}>
          <span className="ot-chart-signed-reason">{value.reason}</span>
        </li>)}
    </ol>
    <div className="ot-chart-signed-axis" aria-hidden="true">
      <span className="ot-chart-signed-tick ot-chart-signed-tick--min">{amount(domain.min)}</span>
      <span className="ot-chart-signed-tick ot-chart-signed-tick--zero">0</span>
      <span className="ot-chart-signed-tick ot-chart-signed-tick--max">{amount(domain.max)}</span>
    </div>
  </div>;
}

/**
 * The two visible lines of evidence that the design shows as text only (Figma boards 24:24, 24:31,
 * 24:38, 24:52, 24:108, 24:122 and the statement of 24:143): the headline, and the details joined in
 * order. Strings pass through unchanged; no number is parsed, rounded or derived.
 */
export function evidenceStatementText({ tone, headline, details }: EvidenceStatement): EvidenceStatementText {
  if (!evidenceStatementTones.includes(tone)) refuse('A declared statement tone is required.');
  requireText(headline, 'statement headline');
  if (!Array.isArray(details) || details.length === 0) refuse('A chart statement needs at least one detail.');
  for (const detail of details) requireText(detail, 'statement detail');
  return { tone, title: headline, body: details.join(statementSeparator) };
}

export function ChartEvidenceNotice(statement: EvidenceStatement) {
  return <Notice {...evidenceStatementText(statement)} />;
}
