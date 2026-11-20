import { createRoot } from 'react-dom/client';
import { EffortLedger } from '../../../features/operator/effort-ledger';
import { historyEntries, referenceEntries } from '../../../features/operator/effort-ledger.fixtures';

export type LedgerFixture = 'reference' | 'history' | 'revoked';
declare global { interface Window { showEffortLedger(name: LedgerFixture): void } }
const root = createRoot(document.getElementById('root')!);
window.showEffortLedger = name => root.render(<main><EffortLedger {...(name === 'revoked' ? { access: 'revoked' as const } : {
  access: 'authorized' as const, entries: name === 'history' ? historyEntries : referenceEntries,
})} /></main>);
