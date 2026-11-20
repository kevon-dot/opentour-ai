import { EvidenceRecords } from '../../../components/foundation/data-display/evidence-records';
import './verifier-qualification.css';

export type SliceComparison =
  | { disclosure: 'withheld' }
  | { disclosure: 'authorized'; samples?: number; agreement?: string; disagreement?: string };
export interface QualificationSlice {
  key: string;
  label: string;
  result: string;
  reason: string;
  comparison?: SliceComparison;
}
export interface VerifierQualificationProps {
  revision: string;
  slices: readonly QualificationSlice[];
  impacts: readonly { key: string; resource: string; state: string; nextStep: string }[];
}

export function VerifierQualification({ revision, slices, impacts }: VerifierQualificationProps) {
  const measurements = slices.flatMap(({ key, label, comparison }) => {
    if (comparison?.disclosure !== 'authorized') return [];
    const supplied = [
      ['Samples', comparison.samples], ['Agreement', comparison.agreement], ['Disagreement', comparison.disagreement],
    ] as const;
    return supplied.flatMap(([measure, value]) => value === undefined ? []
      : [{ key: key + '-' + measure, cells: [label, measure, String(value)] }]);
  });
  return <div className="ot-verifier-qualification">
    <h2>{revision}</h2>
    <EvidenceRecords title="Verifier qualification" columns={['Slice', 'Result', 'Reason']}
      rows={slices.map(slice => ({ key: slice.key, cells: [slice.label, slice.result, slice.reason] }))} />
    {measurements.length > 0 && <EvidenceRecords title="Comparison evidence" columns={['Slice', 'Measure', 'Value']} rows={measurements} />}
    <EvidenceRecords title="Requalification impact" columns={['Resource', 'State', 'Next step']}
      rows={impacts.map(impact => ({ key: impact.key, cells: [impact.resource, impact.state, impact.nextStep] }))} />
  </div>;
}
