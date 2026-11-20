import { flushSync } from 'react-dom';
import { createRoot } from 'react-dom/client';
import { BlockingReasons } from '../../../features/autonomy/blocking-reasons';
import { blockerFixtures, type BlockerFixture } from '../../../features/autonomy/blocking-reasons.fixtures';

declare global { interface Window { showBlockerFixture(state: BlockerFixture): void } }
const root = createRoot(document.querySelector('main')!);
window.showBlockerFixture = state => flushSync(() => root.render(<BlockingReasons {...blockerFixtures[state]} />));
window.showBlockerFixture('three');
