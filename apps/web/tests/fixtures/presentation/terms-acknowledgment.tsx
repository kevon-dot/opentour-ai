import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Button } from '../../../components/foundation/tokens';
import { TermsAcknowledgment, TermsDocumentLink, useTermsAcknowledgment, type PublishedTerms } from '../../../features/account-entry/terms-acknowledgment';
import './terms-acknowledgment.css';

export const generatedTerms: PublishedTerms = {
  revision: '2026-11-01', document_url: 'https://terms.example/november', effective_at: '2026-11-01T00:00:00Z',
};
function Fixture({ current, onReload }: { current: PublishedTerms | null; onReload: () => void }) {
  const choice = useTermsAcknowledgment(current);
  const [name, setName] = useState('Acme Labs');
  return <>
    <header>OpenTour</header>
    <main className="terms-fixture"><h1>Welcome to OpenTour</h1>
      <section className="fixture-steps"><h2>Setup</h2>
        <ol><li>1 Organization · Current</li><li>2 Workspace · Next</li><li>3 Project · Next</li></ol>
      </section>
      {!choice.changed && <TermsDocumentLink acknowledgment={choice} />}
      <section className="fixture-organization"><h2>Create organization</h2>
        <p>Create the organization that will own your workspaces.</p>
        <label className="fixture-name">Organization name · Required
          <input value={name} onChange={event => setName(event.target.value)} /><small>1–120 characters</small>
        </label>
        <TermsAcknowledgment acknowledgment={choice} onReload={onReload} />
        {choice.changed && <TermsDocumentLink acknowledgment={choice} />}
        <Button label="Create organization" disabled={!name.trim() || choice.acceptedRevision === null} />
      </section>
      <footer>SIMULATED DESIGN DATA · Not an executed OpenTour result</footer>
    </main>
  </>;
}
const root = createRoot(document.querySelector('#root')!);
function render(current: PublishedTerms | null) {
  root.render(<Fixture current={current} onReload={() => render(generatedTerms)} />);
}
window.addEventListener('supply-terms', event => render((event as CustomEvent<PublishedTerms | null>).detail));
render(generatedTerms);
