import { flushSync } from 'react-dom';
import { createRoot } from 'react-dom/client';
import { ComparatorEvidence } from '../../../features/product-surfaces/repair-comparator/comparator-evidence';
import { comparatorFixtures, type ComparatorFixture } from '../../../features/product-surfaces/repair-comparator/comparator-evidence.fixtures';
declare global { interface Window { showComparatorFixture(state: ComparatorFixture): void } }

const root = createRoot(document.querySelector('main')!);
window.showComparatorFixture = state => flushSync(() => root.render(<ComparatorEvidence {...comparatorFixtures[state]} />));
window.showComparatorFixture('tied');
