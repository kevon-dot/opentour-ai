/** Presentation inputs only. No prop represents server authority or an operation receipt. */

/**
 * Fill colour only, named as the design names it. A tone never states a condition, a status or a
 * direction; the caller's visible text does.
 */
export type MetricTone = 'blue' | 'mint' | 'lilac' | 'amber';
export type TableCellTone = 'default' | 'blue' | 'mint' | 'lilac' | 'amber' | 'coral';

/**
 * One measured quantity. Every string is visible, required and already formatted by the caller;
 * none is markup and none is parsed. A value that is unknown or missing is written as such in
 * `value` with the reason in `detail`: there is no numeric input, so nothing can default to zero.
 */
export interface MetricProps {
  tone: MetricTone;
  /** What is measured. */
  title: string;
  /** The value with its unit, or the explicit unknown wording. */
  value: string;
  /** Numerator and denominator, the observation context, or the reason the value is unknown. */
  detail: string;
}

/**
 * One cell of a native table row. The component renders the cell element itself, so it is only
 * valid as a child of `<tr>`. An unknown value is written as visible text; a cell is never blank.
 */
export interface TableCellProps {
  value: string;
  tone?: TableCellTone;
  /** `column` or `row` renders a header cell scoped to that column or row; omitted renders a data cell. */
  header?: 'column' | 'row';
}

/** A link to one destination. The caller places it in a navigation landmark and supplies its label. */
export interface NavItemProps {
  label: string;
  href: string;
  /** The destination is the page being viewed. */
  current?: boolean;
}
