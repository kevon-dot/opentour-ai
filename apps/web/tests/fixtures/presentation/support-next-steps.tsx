import { flushSync } from 'react-dom';
import { createRoot } from 'react-dom/client';
import { SupportNextSteps } from '../../../features/product-surfaces/support-center/support-next-steps';
import { supportFixtures, type SupportFixture } from '../../../features/product-surfaces/support-center/support-next-steps.fixtures';

declare global { interface Window { showSupportFixture(name: SupportFixture): void } }

const root = createRoot(document.querySelector('main')!);
window.showSupportFixture = name => {
  flushSync(() => root.render(<SupportNextSteps {...supportFixtures[name]} />));
};
window.showSupportFixture('case41');
