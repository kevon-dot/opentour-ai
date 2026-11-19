import { createRoot } from 'react-dom/client';
import { useState } from 'react';
import { DifferenceReview } from '../../../components/foundation/forms/difference-review';

function ComparisonGallery() {
  const [choices, setChoices] = useState<string[]>([]);
  const record = (field: string, choice: 'current' | 'draft') => setChoices(previous => [...previous, field + ':' + choice]);
  return <>
    <DifferenceReview fieldId="maximum-attempts" fieldName="Maximum attempts" draftRevision="revision 7"
      currentRevision="revision 8" draftValue="3" currentValue="5" onChoice={record} />
    <DifferenceReview fieldId="retry-delay" fieldName="Retry delay for this generated field"
      draftRevision="revision 007" currentRevision="revision 008" draftValue={'  0003?  \n' + 'unparsed'.repeat(18)}
      currentValue=" 0005 " onChoice={record} />
    <output aria-label="Requested choices">{choices.join(', ')}</output>
  </>;
}
createRoot(document.querySelector('main')!).render(<ComparisonGallery />);
