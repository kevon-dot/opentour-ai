'use client';

import { useId } from 'react';
import { TableCell } from '../tokens/display';
import { chartFormat } from './chart-evidence';
import type { ChartAbsence, ObservedCount } from './chart-evidence.types';
import './category-chart.css';

type Count = ObservedCount | ChartAbsence;
interface Category { key: string; label: string; value: Count }

export interface CategoryChartProps {
  /** Complete authorized categories, in the provider's display order. */
  categories: readonly Category[];
  totalCases: number;
  visibleCategoryLimit: number;
  /** Provider's aggregate of the tail, required only when the categories overflow. */
  other?: Count;
  expanded: boolean;
  onExpandedChange: (expanded: boolean) => void;
}

function exact(value: Count) {
  if (value?.status === 'observed') return chartFormat.count(value.count);
  if ((value?.status !== 'unknown' && value?.status !== 'missing') || !value.reason?.trim()) {
    throw new Error('A category requires an observed count or a reason for its absence.');
  }
  return (value.status === 'unknown' ? 'Unknown' : 'Unavailable') + ' · ' + value.reason;
}

function checkTotal(values: readonly Count[], total: number) {
  const observed = values.reduce((sum, value) => sum + (value.status === 'observed' ? value.count : 0), 0);
  if (observed > total || (values.every(value => value.status === 'observed') && observed !== total)) {
    throw new Error('Category counts disagree with the supplied aggregate.');
  }
}

export function CategoryChart({ categories, totalCases, visibleCategoryLimit, other, expanded, onExpandedChange }: CategoryChartProps) {
  const id = useId();
  const total = chartFormat.count(totalCases);
  if (!Number.isSafeInteger(visibleCategoryLimit) || visibleCategoryLimit < 1) {
    throw new Error('The visible category limit must be a positive integer.');
  }
  if (new Set(categories.map(category => category.key)).size !== categories.length ||
      categories.some(category => !category.key.trim() || !category.label.trim())) {
    throw new Error('Categories require unique keys and visible labels.');
  }

  const rows = categories.map(category => ({ ...category, key: 'category-' + category.key, text: exact(category.value) }));
  checkTotal(rows.map(row => row.value), totalCases);
  const overflow = categories.length > visibleCategoryLimit;
  if (overflow !== (other !== undefined)) throw new Error('Supply Other only for an overflowing category chart.');
  const members = overflow ? rows.slice(visibleCategoryLimit - 1) : [];
  const visible = rows.slice(0, overflow ? visibleCategoryLimit - 1 : rows.length);
  if (other !== undefined) {
    const text = exact(other);
    if (other.status === 'observed') checkTotal(members.map(row => row.value), other.count);
    visible.push({ key: 'aggregate', label: 'Other', value: other, text });
    checkTotal(visible.map(row => row.value), totalCases);
  }
  const showAll = expanded && overflow;
  const tableRows = showAll ? rows : visible;
  const tableName = showAll ? 'All categories' : 'Accessible data table';

  return <section className="ot-category" aria-labelledby={id}>
    <h2 id={id} className="ot-chart-title">Cases by category</h2>
    <figure aria-labelledby={id}>
      <figcaption>Learning · {total} {totalCases === 1 ? 'case' : 'cases'}</figcaption>
      {visible.length === 0 ? <p>No categories</p> : <ul>{visible.map(row => <li key={row.key}>
        <span>{row.label} {row.text}</span>
        <div className="ot-category-track" aria-hidden="true">
          {row.value.status === 'observed' && row.value.count > 0 &&
            <span className="ot-category-mark" style={{ width: (100 * row.value.count / totalCases) + '%' }} />}
        </div>
      </li>)}</ul>}
    </figure>
    <div className="ot-category-data">
      <p id={id + '-caption'}>{tableName}<span className="ot-category-hint"> — scroll horizontally</span></p>
      <div id={id + '-table'} role="region" aria-labelledby={id + '-caption'} tabIndex={0}>
        <table aria-label={tableName + ' · cases'}>
          <thead><tr><TableCell header="column" value="Measure" tone="blue" /><TableCell header="column" value="Exact value" tone="blue" /></tr></thead>
          <tbody>{tableRows.map(row => <tr key={row.key}>
            <TableCell header="row" value={row.label} /><TableCell value={row.text} />
          </tr>)}
          {overflow && !showAll && <tr>
            <TableCell header="row" value="Other includes" />
            <TableCell value={members.map(row => row.label + ' ' + row.text).join('; ')} />
          </tr>}</tbody>
        </table>
      </div>
    </div>
    {overflow && <button className="ot-button ot-button--secondary" type="button" aria-expanded={showAll}
      aria-controls={id + '-table'} onClick={() => onExpandedChange(!showAll)}>
      {showAll ? 'Show category summary' : 'View all categories'}
    </button>}
  </section>;
}
