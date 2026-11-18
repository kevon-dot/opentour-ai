import { useId } from 'react';
import { Button } from '../tokens';
import { requireLabel } from '../tokens/require-label';
import './difference-review.css';

export interface DifferenceReviewProps {
  fieldId: string;
  fieldName: string;
  draftRevision: string;
  draftValue: string;
  currentRevision: string;
  currentValue: string;
  onChoice: (fieldId: string, choice: 'current' | 'draft') => void;
}

export function DifferenceReview({ fieldId, fieldName, draftRevision, draftValue,
  currentRevision, currentValue, onChoice }: DifferenceReviewProps) {
  const id = useId();
  requireLabel(fieldId, 'field identity');
  requireLabel(fieldName, 'field name');
  requireLabel(draftRevision, 'draft revision label');
  requireLabel(currentRevision, 'current revision label');
  return <section className="ot-difference-review" aria-labelledby={id}>
    <h3 id={id}>{fieldName}</h3>
    <div className="ot-difference-values">
      <div role="group" aria-labelledby={id + '-draft'}>
        <span id={id + '-draft'}>Your draft · {draftRevision}</span>{': '}<span>{draftValue}</span>
      </div>
      <div role="group" aria-labelledby={id + '-current'}>
        <span id={id + '-current'}>Current settings · {currentRevision}</span>{': '}<span>{currentValue}</span>
      </div>
    </div>
    <div className="ot-difference-actions">
      <Button variant="secondary" label="Keep current value" onPress={() => onChoice(fieldId, 'current')} />
      <Button variant="secondary" label="Use my draft" onPress={() => onChoice(fieldId, 'draft')} />
    </div>
  </section>;
}
