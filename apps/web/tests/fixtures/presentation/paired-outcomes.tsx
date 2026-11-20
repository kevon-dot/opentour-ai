import { createRoot } from 'react-dom/client';
import { PairedOutcomes } from '../../../components/foundation/data-display/paired-outcomes';
import { pairedFixtures } from '../../../components/foundation/data-display/paired-outcomes.fixtures';

export type PairedFixtureName = keyof typeof pairedFixtures;
declare global { interface Window { showPairedFixture(name: PairedFixtureName): void } }
const root = createRoot(document.getElementById('root')!);
window.showPairedFixture = name => root.render(<>
  <header><p>OpenTour　/　Customer workspace</p><small>Acme Labs / Production</small></header>
  <main><h1>Evidence and comparison</h1><p>Demo fixture · Receipt-status workflow · Nov 1–7</p>
    <PairedOutcomes {...pairedFixtures[name]} />
  </main>
</>);
