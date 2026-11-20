import { useId } from 'react';
import { LabelledRecordGrid } from '../../../components/foundation/data-display/relationship-context';
import '../../../styles/product-tokens.css';
import './support-next-steps.css';

type ResourceView = Readonly<{ resource: string; state: string; nextStep: string }>;

export interface SupportNextStepsProps {
  supportCase: (ResourceView & { owner?: string | null; nextUpdate?: string | null }) | null;
  incident: ResourceView | null;
  additionalGrant: ResourceView | null;
  enforcement?: string | null;
}

/** Each input is a separately authorized, current display projection. */
export function SupportNextSteps({ supportCase, incident, additionalGrant, enforcement }: SupportNextStepsProps) {
  const headingId = useId();
  const projections = [
    ['case', supportCase], ['incident', incident], ['grant', additionalGrant],
  ] as const;
  const rows = projections.flatMap(([key, view]) => view === null ? [] : [{
    key, cells: [view.resource, view.state, view.nextStep],
  }]);
  const details = [
    ['Owner', supportCase?.owner], ['Next update', supportCase?.nextUpdate], ['Access enforcement', enforcement],
  ] as const;
  const visibleDetails = details.flatMap(([label, value]) => value ? [{ label, value }] : []);
  if (!rows.length && !visibleDetails.length) return null;
  return <section className="ot-support-next-steps" aria-labelledby={headingId}>
    <h2 id={headingId}>Support and incident</h2>
    {rows.length > 0 && <LabelledRecordGrid name={{ 'aria-labelledby': headingId }}
      columns={['Resource', 'State', 'Next step']} rows={rows} />}
    {visibleDetails.length > 0 && <dl className="ot-support-details">
      {visibleDetails.map(({ label, value }) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
    </dl>}
  </section>;
}
