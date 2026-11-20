import { useId } from 'react';
import { LabelledRecordGrid } from './relationship-context';
import './evidence-records.css';

export function EvidenceRecords({ title, columns, rows }: {
  title: string;
  columns: readonly string[];
  rows: readonly { key: string; cells: readonly string[] }[];
}) {
  const heading = useId();
  return <section className="ot-evidence-records" aria-labelledby={heading}>
    <h3 id={heading}>{title}</h3>
    <LabelledRecordGrid name={{ 'aria-labelledby': heading }} columns={columns} rows={rows} />
  </section>;
}
