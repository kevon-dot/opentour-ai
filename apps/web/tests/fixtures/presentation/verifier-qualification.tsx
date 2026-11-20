import { flushSync } from 'react-dom';
import { createRoot } from 'react-dom/client';
import { VerifierQualification } from '../../../features/product-surfaces/verifier-console/verifier-qualification';
import { verifierFixtures, type VerifierFixture } from '../../../features/product-surfaces/verifier-console/verifier-qualification.fixtures';

declare global { interface Window { showVerifierFixture(state: VerifierFixture): void } }
const root = createRoot(document.querySelector('main')!);
window.showVerifierFixture = state => flushSync(() => root.render(<VerifierQualification {...verifierFixtures[state]} />));
window.showVerifierFixture('exact');
