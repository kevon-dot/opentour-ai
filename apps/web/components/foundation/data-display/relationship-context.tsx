'use client';

import { useId, useLayoutEffect, useRef, useState, type ReactElement } from 'react';
import { Notice } from '../tokens/notice';
import { TableCell } from '../tokens/display';
import { requireLabel } from '../tokens/require-label';
import type {
  RelationshipContextProps, RelationshipRecordRow, SelectedNodeContext, SelectedRelationshipRow,
} from './relationship-context.types';
import '../../../styles/product-tokens.css';
import './relationship-context.css';

export type {
  NonEmptyRows, RelationshipContextProps, RelationshipRecordRow, RelationshipVersionTone,
  SelectedNodeContext, SelectedRelationshipRow,
} from './relationship-context.types';

/** Fixed public copy. Empty and redacted states are not caller-overridable. */
export const relationshipCopy = {
  records: { heading: 'Details', columns: ['Node', 'Relationship', 'Version'] },
  selected: { heading: 'Selected relationship', columns: ['Source', 'Relation', 'Target', 'Version', 'Validity'], referenceLabel: 'Exact reference' },
  empty: {
    heading: 'No visible relationships',
    body: 'Nothing is currently visible in this authorized view. This does not establish that the item has no dependencies.',
    total: 'Total relationships: Not available',
    columns: ['Relationship', 'Current view'],
    row: ['No visible relationships', 'No permitted rows returned'],
  },
  redacted: {
    title: 'Some relationship details are restricted',
    body: 'Your authorized view remains available. Restricted names, types and counts are not shown.',
    columns: ['Source', 'Relationship', 'Target', 'Access'],
    row: ['Restricted item', 'Redacted relationship', 'Current visible item', 'Permission redacted'],
  },
} as const;

const scrollHint = ' — scroll horizontally';

/**
 * Refuses any key outside the declared shape, so undeclared (possibly hidden) data cannot ride along.
 * Diagnostics use only the fixed internal `name` label: a caller key may itself be restricted data,
 * so neither unexpected keys nor any caller value is ever echoed.
 */
function closedShape(value: unknown, required: readonly string[], optional: readonly string[], name: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new Error(`A ${name} object is required.`);
  const record = value as Record<string, unknown>;
  for (const key of Object.keys(record)) {
    if (!required.includes(key) && !optional.includes(key)) throw new Error(`Undeclared ${name} fields are refused.`);
  }
  for (const key of required) if (!(key in record)) throw new Error(`A required ${name} field is missing.`);
  return record;
}

function closedRows<Row>(rows: unknown, fields: readonly string[], optional: readonly string[], name: string): readonly Row[] {
  if (!Array.isArray(rows) || rows.length === 0) {
    throw new Error(`At least one ${name} is required; state an empty view explicitly instead.`);
  }
  const keys = new Set<string>();
  for (const row of rows) {
    const record = closedShape(row, ['key', ...fields], optional, name);
    requireLabel(record.key as string, `${name} key`);
    if (keys.has(record.key as string)) throw new Error(`Duplicate ${name} key.`);
    keys.add(record.key as string);
    for (const field of fields) requireLabel(record[field] as string, `${name} ${field}`);
  }
  return rows as readonly Row[];
}

interface GridRow { key: string; cells: readonly string[] }

/**
 * One relationship grid, drawn as a native table on wider containers and as labelled stacked
 * records on narrow ones. A container query displays exactly one; the other is `display: none`
 * and therefore absent from the accessibility tree.
 */
function RelationshipGrid({ name, columns, rows, kind }: {
  name: { 'aria-labelledby': string } | { 'aria-label': string };
  columns: readonly string[]; rows: readonly GridRow[]; kind: 'selected' | 'empty' | 'redacted';
}) {
  return <div className={`ot-relationship-grid ot-relationship-grid--${kind}`}>
    <table className="ot-relationship-table" {...name}>
      <thead><tr>{columns.map(column => <th key={column} scope="col">{column}</th>)}</tr></thead>
      <tbody>{rows.map(row => <tr key={row.key}>
        <th scope="row">{row.cells[0]}</th>
        {row.cells.slice(1).map((cell, index) => <td key={columns[index + 1]}>{cell}</td>)}
      </tr>)}</tbody>
    </table>
    <ul className="ot-relationship-stack" {...name}>
      {rows.map(row => <li key={row.key} className="ot-relationship-record"><dl>
        {columns.map((column, index) => <div key={column} className="ot-relationship-line">
          <dt>{`${column}:`}</dt>{' '}<dd>{row.cells[index]}</dd>
        </div>)}
      </dl></li>)}
    </ul>
  </div>;
}

/** The records table scrolls inside a labelled keyboard stop only while it is actually wider than its space. */
function useHorizontalOverflow() {
  const ref = useRef<HTMLDivElement>(null);
  const [scrollable, setScrollable] = useState(false);
  useLayoutEffect(() => {
    const node = ref.current!;
    const measure = () => setScrollable(node.scrollWidth > node.clientWidth);
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    if (node.firstElementChild) observer.observe(node.firstElementChild);
    measure();
    return () => observer.disconnect();
  }, []);
  return [ref, scrollable] as const;
}

function RecordsView({ rows, observation }: { rows: readonly RelationshipRecordRow[]; observation: string }) {
  const titleId = useId();
  const hintId = useId();
  const [ref, scrollable] = useHorizontalOverflow();
  const copy = relationshipCopy.records;
  return <div className="ot-relationship-records">
    <h2 className="ot-relationship-section-title">
      <span id={titleId}>{copy.heading}</span>
      {scrollable && <span id={hintId} className="ot-relationship-scroll-hint">{scrollHint}</span>}
    </h2>
    <div ref={ref} className="ot-relationship-scroll"
      {...(scrollable ? { role: 'region', tabIndex: 0, 'aria-labelledby': `${titleId} ${hintId}` } : {})}>
      <table className="ot-relationship-records-table" aria-labelledby={titleId}>
        <colgroup><col className="ot-relationship-col-node" /><col className="ot-relationship-col-relationship" /><col className="ot-relationship-col-version" /></colgroup>
        <thead><tr>{copy.columns.map(column => <TableCell key={column} header="column" tone="blue" value={column} />)}</tr></thead>
        <tbody>{rows.map(row => <tr key={row.key}>
          <TableCell header="row" value={row.node} />
          <TableCell value={row.relationship} />
          <TableCell value={row.version} tone={row.versionTone === 'mint' ? 'mint' : 'default'} />
        </tr>)}</tbody>
      </table>
    </div>
    <p className="ot-relationship-observation">{observation}</p>
  </div>;
}

function SelectedDetails({ selected }: { selected: SelectedNodeContext }) {
  const titleId = useId();
  return <div className="ot-relationship-selected" aria-labelledby={titleId} role="group">
    <h3 id={titleId} className="ot-relationship-selected-title">{selected.title}</h3>
    <p className="ot-relationship-selected-note">{selected.note}</p>
    <dl className="ot-relationship-reference">
      <dt>{relationshipCopy.selected.referenceLabel}</dt>
      <dd className="ot-relationship-reference-value">{selected.reference}</dd>
      <dd className="ot-relationship-reference-detail">{selected.referenceDetail}</dd>
    </dl>
  </div>;
}

function SelectedView({ rows, selected }: { rows: readonly SelectedRelationshipRow[]; selected: SelectedNodeContext | null }) {
  const headingId = useId();
  const copy = relationshipCopy.selected;
  return <>
    <h2 id={headingId} className="ot-relationship-section-title">{copy.heading}</h2>
    <RelationshipGrid kind="selected" name={{ 'aria-labelledby': headingId }} columns={copy.columns}
      rows={rows.map(row => ({ key: row.key, cells: [row.source, row.relation, row.target, row.version, row.validity] }))} />
    {selected && <SelectedDetails selected={selected} />}
  </>;
}

function EmptyView() {
  const headingId = useId();
  const copy = relationshipCopy.empty;
  return <>
    <div className="ot-relationship-empty">
      <h2 id={headingId} className="ot-relationship-empty-title">{copy.heading}</h2>
      <p className="ot-relationship-empty-body">{copy.body}</p>
      <p className="ot-relationship-empty-total">{copy.total}</p>
    </div>
    <RelationshipGrid kind="empty" name={{ 'aria-labelledby': headingId }} columns={copy.columns} rows={[{ key: 'empty', cells: copy.row }]} />
  </>;
}

function RedactedView() {
  const copy = relationshipCopy.redacted;
  return <>
    <Notice tone="hold" title={copy.title} body={copy.body} />
    <RelationshipGrid kind="redacted" name={{ 'aria-label': copy.title }} columns={copy.columns} rows={[{ key: 'redacted', cells: copy.row }]} />
  </>;
}

/**
 * Read-only relationship display for already-authorized rows. It performs no loading, authorization,
 * graph expansion, clipboard, storage or navigation, and offers no actions. An absent selection
 * renders no details: nothing from an earlier selection is retained.
 */
export function RelationshipContext(props: RelationshipContextProps) {
  const view = (props as { view?: unknown } | null)?.view;
  let content: ReactElement;
  if (view === 'records') {
    const record = closedShape(props, ['view', 'rows', 'observation'], [], 'records view');
    const rows = closedRows<RelationshipRecordRow>(record.rows, ['node', 'relationship', 'version'], ['versionTone'], 'relationship record');
    for (const row of rows) {
      if (row.versionTone !== undefined && row.versionTone !== 'default' && row.versionTone !== 'mint') {
        throw new Error('A relationship version tone is `default`, `mint` or omitted.');
      }
    }
    requireLabel(record.observation as string, 'relationship observation');
    content = <RecordsView rows={rows} observation={record.observation as string} />;
  } else if (view === 'selected') {
    const record = closedShape(props, ['view', 'rows', 'selected'], [], 'selected view');
    const rows = closedRows<SelectedRelationshipRow>(record.rows, ['source', 'relation', 'target', 'version', 'validity'], [], 'selected relationship');
    let selected: SelectedNodeContext | null = null;
    if (record.selected !== null) {
      const details = closedShape(record.selected, ['title', 'note', 'reference', 'referenceDetail'], [], 'selected node context');
      for (const field of ['title', 'note', 'reference', 'referenceDetail']) requireLabel(details[field] as string, `selected node ${field}`);
      selected = details as unknown as SelectedNodeContext;
    }
    content = <SelectedView rows={rows} selected={selected} />;
  } else if (view === 'empty') {
    closedShape(props, ['view'], [], 'empty view');
    content = <EmptyView />;
  } else if (view === 'redacted') {
    closedShape(props, ['view'], [], 'redacted view');
    content = <RedactedView />;
  } else {
    throw new Error('A declared relationship view is required.');
  }
  return <div className={`ot-relationship-context ot-relationship-context--${view}`}>{content}</div>;
}
