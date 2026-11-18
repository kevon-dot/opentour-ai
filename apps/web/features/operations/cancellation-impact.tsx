import { useId } from 'react';
import { TableCell } from '../../components/foundation/tokens/display';
import './cancellation-impact.css';

export interface CancellationImpactProps {
  accruedLiability: string | null;
  reservation: string | null;
  lastHeartbeat: string | null;
  heartbeatAge: string | null;
  retainedArtifact: string | null;
  effects: readonly { id: string; label: string; disposition: string | null }[];
}

interface ImpactRow {
  id: string;
  label: string;
  value: string | null;
}

function ImpactTable({ title, columns, rows }: {
  title: string;
  columns: readonly [string, string];
  rows: readonly ImpactRow[];
}) {
  const titleId = useId();
  return <div className="ot-impact-block">
    <h3 id={titleId}>{title}<span className="ot-impact-scroll-hint"> — scroll horizontally</span></h3>
    <div className="ot-impact-scroll" role="region" aria-labelledby={titleId} tabIndex={0}>
      <table aria-label={title}>
        <thead><tr>
          <TableCell value={columns[0]} tone="blue" header="column" />
          <TableCell value={columns[1]} tone="blue" header="column" />
        </tr></thead>
        <tbody>{rows.map(row => <tr key={row.id}>
          <TableCell value={row.label} header="row" />
          <TableCell value={row.value?.trim() ? row.value : 'Unknown'} />
        </tr>)}</tbody>
      </table>
    </div>
  </div>;
}

/** Displays supplied observations verbatim; missing observations are explicitly unknown. */
export function CancellationImpact({ accruedLiability, reservation, lastHeartbeat, heartbeatAge, retainedArtifact, effects }: CancellationImpactProps) {
  const impact = [
    { id: 'liability', label: 'Accrued liability', value: accruedLiability },
    { id: 'reservation', label: 'Reservation', value: reservation },
    { id: 'heartbeat', label: 'Last heartbeat', value: lastHeartbeat },
    { id: 'age', label: 'Heartbeat age', value: heartbeatAge },
    { id: 'artifact', label: 'Retained artifact', value: retainedArtifact },
  ];
  return <div className="ot-cancellation-impact">
    <ImpactTable title="Cancellation impact" columns={['Impact', 'Current value']} rows={impact} />
    <ImpactTable title="Remaining effects" columns={['Retained or delivered effect', 'Disposition']}
      rows={effects.map(effect => ({ id: effect.id, label: effect.label, value: effect.disposition }))} />
  </div>;
}
