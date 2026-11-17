import type { MetricProps, MetricTone, NavItemProps, TableCellProps, TableCellTone } from './display.contracts';
import { requireLabel } from './require-label';
import '../../../styles/product-tokens.css';
import './display.css';

export type { MetricProps, MetricTone, NavItemProps, TableCellProps, TableCellTone } from './display.contracts';

/** Declared tones in the Figma variant order of component sets 4:67 (Metric) and 4:80 (Table cell). */
export const metricTones: readonly MetricTone[] = ['blue', 'mint', 'lilac', 'amber'];
export const tableCellTones: readonly TableCellTone[] = ['default', 'blue', 'mint', 'lilac', 'amber', 'coral'];

/**
 * A metric card. The description list ties the title to its value and detail without ARIA, and is
 * complete on its own, so a card can stand alone or beside others in any container.
 */
export function Metric({ tone, title, value, detail }: MetricProps) {
  if (!metricTones.includes(tone)) throw new Error('A declared metric tone is required.');
  requireLabel(title, 'metric title');
  requireLabel(value, 'metric value');
  requireLabel(detail, 'metric detail');
  return <dl className={`ot-metric ot-metric--${tone}`}>
    <dt className="ot-metric-title">{title}</dt>
    <dd className="ot-metric-value">{value}</dd>
    <dd className="ot-metric-detail">{detail}</dd>
  </dl>;
}

/**
 * A native table cell. The `td` or `th` carries the table semantics and no paint; the frame inside
 * it is the designed cell, which hugs its own text and sits at the top of a taller row.
 */
export function TableCell({ value, tone = 'default', header }: TableCellProps) {
  if (!tableCellTones.includes(tone)) throw new Error('A declared table cell tone is required.');
  if (header !== undefined && header !== 'column' && header !== 'row') {
    throw new Error('A table cell header is `column`, `row` or omitted.');
  }
  requireLabel(value, 'table cell value');
  const frame = <div className={`ot-table-cell-frame ot-table-cell-frame--${tone}`}>
    <span className="ot-table-cell-value">{value}</span>
  </div>;
  if (header === undefined) return <td className="ot-table-cell">{frame}</td>;
  return <th scope={header === 'column' ? 'col' : 'row'} className="ot-table-cell">{frame}</th>;
}

/**
 * A navigation link. It is a plain anchor with no routing dependency; `current` is announced with
 * `aria-current="page"`, and that attribute is also what selects the active appearance.
 */
export function NavItem({ label, href, current = false }: NavItemProps) {
  requireLabel(label, 'navigation label');
  if (typeof href !== 'string' || href.trim().length === 0) {
    throw new Error('A navigation item destination is required.');
  }
  return <a className="ot-nav-item" href={href} aria-current={current === true ? 'page' : undefined}>{label}</a>;
}
