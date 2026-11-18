import { createRoot } from 'react-dom/client';
import { flushSync } from 'react-dom';
import {
  RelationshipContext, type NonEmptyRows, type RelationshipContextProps, type RelationshipRecordRow,
  type SelectedNodeContext, type SelectedRelationshipRow,
} from '../../../components/foundation/data-display/relationship-context';

/** Generated OPE-319 presentation fixtures. Synthetic display strings only; no real records. */
export const recordRows: NonEmptyRows<RelationshipRecordRow> = [
  { key: 'policy-v2', node: 'Policy v2', relationship: 'Defines requirement', version: '12' },
  { key: 'case-refund-042', node: 'Case refund-042', relationship: 'Violates requirement', version: '3' },
  { key: 'verified-correction', node: 'Verified correction', relationship: 'Derived from evidence', version: '2' },
  { key: 'dataset-ds-014', node: 'Dataset ds-014', relationship: 'Includes correction', version: 'Sealed', versionTone: 'mint' },
  { key: 'evaluation-eval-014', node: 'Evaluation eval-014', relationship: 'Tests requirement', version: 'Frozen' },
  { key: 'release-r1', node: 'Release r1', relationship: 'Held by retention', version: '1' },
];
export const recordObservation = '6 records · observed 09:42 UTC';

export const selectedRows: NonEmptyRows<SelectedRelationshipRow> = [
  { key: 'correction-dataset', source: 'Correction 204', relation: 'included in', target: 'Dataset 5', version: '3 → 5', validity: 'Selected path' },
  { key: 'dataset-evaluation', source: 'Dataset 5', relation: 'evaluated in', target: 'Evaluation 41', version: '5 → 41', validity: 'Affected' },
  { key: 'unknown-release', source: 'Unknown dependency', relation: 'not reported', target: 'Release 4', version: 'Not available', validity: 'Unknown' },
];
export const selectedContext: SelectedNodeContext = {
  title: 'Correction 204 · version 3',
  note: 'Selected in graph and relationship table',
  reference: 'correction:204 · revision 3',
  referenceDetail: 'Digest sha256: 6f83…d124',
};
/** A second generated selection, used to prove replacement leaves nothing from the first. */
export const alternateContext: SelectedNodeContext = {
  title: 'Dataset 5 · version 5',
  note: 'Generated alternate selection',
  reference: 'dataset:5 · revision 5',
  referenceDetail: 'Digest sha256: generated-alternate',
};

const unbroken = (stem: string) => (stem + '-generated-unbroken').repeat(6);
export const longRecordRows: NonEmptyRows<RelationshipRecordRow> = [
  { key: 'long-node', node: unbroken('Node'), relationship: '<em>caller text is not markup</em>', version: unbroken('Version') },
  { key: 'long-sealed', node: 'Dataset ds-014', relationship: unbroken('Relationship'), version: 'Sealed', versionTone: 'mint' },
];
export const longSelectedRows: NonEmptyRows<SelectedRelationshipRow> = [
  { key: 'long-source', source: unbroken('Source'), relation: '<b>included in</b>', target: unbroken('Target'), version: '3 → 5 → 8 → 13', validity: 'Unknown' },
  { key: 'long-unknown', source: 'Unknown dependency', relation: 'not reported', target: 'Release 4', version: 'Not available', validity: unbroken('Validity') },
];
export const longContext: SelectedNodeContext = {
  title: unbroken('Title'), note: 'Selected in graph and relationship table',
  reference: unbroken('reference'), referenceDetail: 'Digest sha256: ' + unbroken('digest'),
};

export const relationshipFixtures = {
  populated: [{ view: 'records', rows: recordRows, observation: recordObservation }],
  selected: [{ view: 'selected', rows: selectedRows, selected: selectedContext }],
  'selected-alternate': [{ view: 'selected', rows: selectedRows, selected: alternateContext }],
  'selected-cleared': [{ view: 'selected', rows: selectedRows, selected: null }],
  empty: [{ view: 'empty' }],
  redacted: [{ view: 'redacted' }],
  long: [
    { view: 'records', rows: longRecordRows, observation: '2 records · ' + unbroken('observation') },
    { view: 'selected', rows: longSelectedRows, selected: longContext },
  ],
} as const satisfies Record<string, readonly RelationshipContextProps[]>;
export type RelationshipFixture = keyof typeof relationshipFixtures;

/** Local gallery shell: page padding 16/24/32 and the 240px desktop rail drawn in the selected boards. */
const galleryStyles = `
body { margin: 0; background: var(--ot-surface-canvas); }
.relationship-gallery { display: grid; grid-template-columns: minmax(0, 1fr); min-block-size: 100vh; }
.relationship-gallery-rail { display: none; background: var(--ot-surface-nav); }
.relationship-gallery-main { display: flex; flex-direction: column; gap: 32px; min-inline-size: 0; padding: 16px; }
@media (min-width: 768px) { .relationship-gallery-main { padding: 24px; } }
@media (min-width: 1280px) {
  .relationship-gallery-main { padding: 32px; }
  .relationship-gallery[data-rail] { grid-template-columns: 240px minmax(0, 1fr); }
  .relationship-gallery[data-rail] .relationship-gallery-rail { display: block; }
}`;

function Gallery({ name }: { name: RelationshipFixture }) {
  const rail = name.startsWith('selected');
  return <div className="relationship-gallery" data-rail={rail ? '' : undefined}>
    <div className="relationship-gallery-rail" aria-hidden="true" />
    <main className="relationship-gallery-main">
      {(relationshipFixtures[name] as readonly RelationshipContextProps[]).map((props, index) => <RelationshipContext key={`view-${index}`} {...props} />)}
    </main>
  </div>;
}

declare global { interface Window { showRelationshipFixture(name: RelationshipFixture): void } }

if (typeof document !== 'undefined') {
  const style = document.createElement('style');
  style.textContent = galleryStyles;
  document.head.append(style);
  const root = createRoot(document.querySelector('#root')!);
  // Synchronous so a test can read the DOM immediately after a rerender.
  window.showRelationshipFixture = name => flushSync(() => root.render(<Gallery name={name} />));
}
