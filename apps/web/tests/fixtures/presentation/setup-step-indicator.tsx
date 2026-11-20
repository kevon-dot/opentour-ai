import { createRoot } from 'react-dom/client';
import { SetupStepIndicator, type SetupStep } from '../../../features/account-entry/setup-step-indicator';
import './setup-step-indicator.css';

const root = createRoot(document.getElementById('fixture')!);

function render(currentStep: SetupStep) {
  root.render(<>
    <header>OpenTour</header>
    <main>
      <h1>Welcome to OpenTour</h1>
      <SetupStepIndicator currentStep={currentStep} />
      <p>SIMULATED DESIGN DATA · Not an executed OpenTour result</p>
    </main>
  </>);
}

// Only the generated fixture accepts synthetic caller updates.
window.addEventListener('setup-fixture-current', event => {
  const current = (event as CustomEvent<SetupStep>).detail;
  render(current);
});
render('organization');
