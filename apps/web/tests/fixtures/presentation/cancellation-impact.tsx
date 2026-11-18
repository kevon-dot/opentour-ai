import { createRoot } from 'react-dom/client';
import { CancellationImpact, type CancellationImpactProps } from '../../../features/operations/cancellation-impact';

export const observed: CancellationImpactProps = {
  accruedLiability: 'USD 7.40 accrued · not final',
  reservation: 'res-42 · USD 25.00 maximum',
  lastHeartbeat: 'Nov 12, 09:42 UTC',
  heartbeatAge: '240 seconds at observation',
  retainedArtifact: 'Checkpoint cp-7 · retention policy',
  effects: [
    { id: 'audit', label: 'Sanitized audit receipt', disposition: 'Retained under policy' },
    { id: 'copy', label: 'Previously delivered copy', disposition: 'Outside OpenTour control · cannot be recalled' },
  ],
};

const mount = document.querySelector('main')!;
const values = mount.dataset.adverse ? {
  ...observed, accruedLiability: null, retainedArtifact: 'Checkpoint ' + 'generated-sentinel-'.repeat(15),
} : observed;
createRoot(mount).render(<CancellationImpact {...values} />);
