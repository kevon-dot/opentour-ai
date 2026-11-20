import { createRoot } from 'react-dom/client';
import { Metric, Notice, TableCell } from '../../../components/foundation/tokens';
import { Tooltip } from '../../../components/foundation/tokens/tooltip';
import { ResourceDisclosure, type ResourceDisclosureProps } from '../../../features/access/resource-disclosure';

export const protectedFixture = {
  name: 'Receipt-status workflow · Revision 8',
  count: '300',
  action: 'Start evaluation',
  reason: 'Your role does not include the evaluation capability.',
  help: 'Fixture population receipts-nov-01-07',
} as const;

declare global {
  interface Window {
    showDisclosure(name: ResourceDisclosureProps['disclosure']): void;
    disclosureEvents: { activations: number; returns: number };
  }
}
const root = createRoot(document.getElementById('root')!);
window.disclosureEvents = { activations: 0, returns: 0 };
window.showDisclosure = disclosure => {
  const props: ResourceDisclosureProps = disclosure === 'undisclosed'
    ? { disclosure, onReturnToWorkspace: () => { window.disclosureEvents.returns++; } }
    : { disclosure, actionLabel: protectedFixture.action, safeReason: protectedFixture.reason,
      authorizedContent: <>
        <h1>Benchmark evidence</h1>
        <p className="fixture-workflow">{protectedFixture.name}</p>
        <Notice tone="hold" title="Action unavailable" body="Your role can view this workflow but cannot start an evaluation. Ask a workspace administrator for the evaluation capability." />
        <Metric tone="blue" title="Eligible cases" value={protectedFixture.count} detail="Observed · Nov 1–7" />
        <section className="fixture-evidence">
          <h2>Current authorized evidence</h2>
          <table aria-label="Current authorized evidence">
            <thead><tr><TableCell header="column" tone="blue" value="Measure" /><TableCell header="column" tone="blue" value="Value" /></tr></thead>
            <tbody>
              <tr><TableCell header="row" value="Eligible cases" /><TableCell value={protectedFixture.count} /></tr>
              <tr><TableCell header="row" value="Observation" /><TableCell value="Nov 12, 09:42 UTC" /></tr>
            </tbody>
          </table>
        </section>
      </>,
      relatedAction: <Tooltip description={protectedFixture.help}><button className="ot-button ot-button--secondary" type="button">Refresh view</button></Tooltip>,
    };
  return root.render(<main><form
    onSubmit={event => { event.preventDefault(); window.disclosureEvents.activations++; }}
    onClick={event => {
      if ((event.target as HTMLElement).closest('[aria-disabled="true"]')) window.disclosureEvents.activations++;
    }}
    onKeyDown={event => {
      if ((event.key === 'Enter' || event.key === ' ') && (event.target as HTMLElement).closest('[aria-disabled="true"]')) window.disclosureEvents.activations++;
    }}>
    <ResourceDisclosure {...props} />
  </form></main>);
};
