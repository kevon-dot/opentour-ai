import { createRoot } from 'react-dom/client';
import { OfflineEvidence } from '../../../features/benchmarks/offline-evidence';
import { evidenceFixture, type EvidenceFixture } from '../../../features/benchmarks/offline-evidence.fixtures';

declare global { interface Window { showOfflineEvidence(state: EvidenceFixture): void; localRetryCalls: number } }
const root = createRoot(document.getElementById('root')!);
window.localRetryCalls = 0;
window.showOfflineEvidence = state => root.render(<main><OfflineEvidence
  {...evidenceFixture(state, () => { window.localRetryCalls += 1; })} /></main>);
