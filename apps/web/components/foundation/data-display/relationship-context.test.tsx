import { renderToStaticMarkup } from 'react-dom/server';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import { RelationshipContext, relationshipCopy, type RelationshipContextProps } from './relationship-context';
import {
  alternateContext, recordObservation, recordRows, selectedContext, selectedRows,
} from '../../../tests/fixtures/presentation/relationship-context';

const render = (props: RelationshipContextProps) => renderToStaticMarkup(<RelationshipContext {...props} />);
const decode = (text: string) => text.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
  .replace(/&#x27;/g, "'").replace(/&amp;/g, '&');
const text = (html: string) => decode(html.replace(/<[^>]+>/g, ''));

interface Cell { tag: string; scope: string | null; text: string; markup: string }
/** Every row of the nth native table, as header/data cells with their scope and visible text. */
function table(html: string, index = 0): Cell[][] {
  const markup = [...html.matchAll(/<table\b[\s\S]*?<\/table>/g)][index]?.[0];
  if (!markup) throw new Error('No table rendered');
  return [...markup.matchAll(/<tr>([\s\S]*?)<\/tr>/g)].map(row =>
    [...row[1].matchAll(/<(th|td)\b([^>]*)>([\s\S]*?)<\/\1>/g)].map(cell => ({
      tag: cell[1], scope: /scope="(\w+)"/.exec(cell[2])?.[1] ?? null, text: text(cell[3]), markup: cell[0],
    })));
}
/** Every stacked narrow record, as its visible labelled lines. */
const records = (html: string) => [...html.matchAll(/<li class="ot-relationship-record">([\s\S]*?)<\/li>/g)]
  .map(record => [...record[1].matchAll(/<div class="ot-relationship-line">([\s\S]*?)<\/div>/g)].map(line => text(line[1])));
const interactive = /<(?:button|input|select|textarea|a|dialog|form)\b|tabindex=|role="(?:dialog|alertdialog|alert|button|link)"|aria-live|aria-modal|contenteditable|onclick|href=/i;

/** Generated redaction sentinel; it must never surface in markup, diagnostics or stacks. */
const sentinel = 'generated-restricted-sentinel-7f3a';
/** Renders refused props and returns the actual thrown error and whatever markup was produced. */
function attempt(props: unknown): { error: Error; html: string | null } {
  let html: string | null = null;
  try {
    html = render(props as RelationshipContextProps);
  } catch (caught) {
    assert.ok(caught instanceof Error);
    return { error: caught as Error, html };
  }
  throw new Error('Expected the refused props to throw.');
}

const populated: RelationshipContextProps ={ view: 'records', rows: recordRows, observation: recordObservation };
const selected: RelationshipContextProps = { view: 'selected', rows: selectedRows, selected: selectedContext };

describe('OPE-319 read-only relationship records (16:81 data portion)', () => {
  it('renders the Details heading, native Node/Relationship/Version headers and all six exact rows', () => {
    const html = render(populated);
    assert.match(html, /<h2 class="ot-relationship-section-title"><span id="[^"]+">Details<\/span><\/h2>/);
    const rows = table(html);
    assert.deepEqual(rows[0].map(cell => [cell.tag, cell.scope, cell.text]), [
      ['th', 'col', 'Node'], ['th', 'col', 'Relationship'], ['th', 'col', 'Version']]);
    assert.deepEqual(rows.slice(1).map(row => row.map(cell => cell.text)), [
      ['Policy v2', 'Defines requirement', '12'],
      ['Case refund-042', 'Violates requirement', '3'],
      ['Verified correction', 'Derived from evidence', '2'],
      ['Dataset ds-014', 'Includes correction', 'Sealed'],
      ['Evaluation eval-014', 'Tests requirement', 'Frozen'],
      ['Release r1', 'Held by retention', '1'],
    ]);
    for (const row of rows.slice(1)) assert.deepEqual(row.map(cell => [cell.tag, cell.scope]), [['th', 'row'], ['td', null], ['td', null]]);
    assert.match(html, /<table class="ot-relationship-records-table" aria-labelledby="([^"]+)">/);
    const [, titleId] = /<span id="([^"]+)">Details<\/span>/.exec(html)!;
    assert.ok((html)?.includes(`aria-labelledby="${titleId}"`));
  });

  it('paints blue headers, a single mint cell for the caller-toned Sealed row and preserves the observation', () => {
    const html = render(populated);
    assert.equal((html.match(/ot-table-cell-frame--blue/g))?.length, 3);
    const mint = table(html).flat().filter(cell => cell.markup.includes('ot-table-cell-frame--mint'));
    assert.deepEqual(mint.map(cell => cell.text), ['Sealed']);
    assert.ok((html)?.includes(`<p class="ot-relationship-observation">${recordObservation}</p>`));
  });

  it('excludes search, filter, sort and pagination and exposes no action or tab stop', () => {
    const visible = text(render(populated));
    for (const excluded of ['Search', 'Filter', 'Sort', 'Previous', 'Next']) assert.ok((visible)?.includes(excluded) === false);
    assert.doesNotMatch(render(populated), interactive);
    // The horizontal-scroll region only becomes a labelled keyboard stop once it is measured as scrollable.
    assert.ok((render(populated))?.includes('role="region"') === false);
  });

  it('never infers a tone or meaning from the version text', () => {
    const html = render({ view: 'records', observation: 'generated', rows: [
      { key: 'a', node: 'A', relationship: 'r', version: 'Sealed' },
      { key: 'b', node: 'B', relationship: 'r', version: '12', versionTone: 'mint' },
    ] });
    const cells = table(html).flat().filter(cell => cell.markup.includes('frame--mint'));
    assert.deepEqual(cells.map(cell => cell.text), ['12']);
  });
});

describe('OPE-319 selected relationship table (165:11224/11321/11818/12315)', () => {
  it('associates the five native headers with the three exact rows on wider containers', () => {
    const html = render(selected);
    assert.match(html, /<h2 id="([^"]+)" class="ot-relationship-section-title">Selected relationship<\/h2>/);
    const [, headingId] = /<h2 id="([^"]+)"/.exec(html)!;
    assert.ok((html)?.includes(`<table class="ot-relationship-table" aria-labelledby="${headingId}">`));
    const rows = table(html);
    assert.deepEqual(rows[0].map(cell => [cell.tag, cell.scope, cell.text]),
      ['Source', 'Relation', 'Target', 'Version', 'Validity'].map(name => ['th', 'col', name]));
    assert.deepEqual(rows.slice(1).map(row => row.map(cell => cell.text)), [
      ['Correction 204', 'included in', 'Dataset 5', '3 → 5', 'Selected path'],
      ['Dataset 5', 'evaluated in', 'Evaluation 41', '5 → 41', 'Affected'],
      ['Unknown dependency', 'not reported', 'Release 4', 'Not available', 'Unknown'],
    ]);
    for (const row of rows.slice(1)) assert.deepEqual(row.map(cell => cell.scope), ['row', null, null, null, null]);
  });

  it('provides the same records as labelled narrow stacks with five visible prefixes each', () => {
    const html = render(selected);
    const [, headingId] = /<h2 id="([^"]+)"/.exec(html)!;
    assert.ok((html)?.includes(`<ul class="ot-relationship-stack" aria-labelledby="${headingId}">`));
    assert.deepEqual(records(html), [
      ['Source: Correction 204', 'Relation: included in', 'Target: Dataset 5', 'Version: 3 → 5', 'Validity: Selected path'],
      ['Source: Dataset 5', 'Relation: evaluated in', 'Target: Evaluation 41', 'Version: 5 → 41', 'Validity: Affected'],
      ['Source: Unknown dependency', 'Relation: not reported', 'Target: Release 4', 'Version: Not available', 'Validity: Unknown'],
    ]);
    assert.equal((html.match(/<dt>/g))?.length, 15 + 1);
    assert.ok((html)?.includes('3 → 5'));
    assert.doesNotMatch(html, /3 -&gt; 5|3->5/);
  });

  it('renders the exact selected-node context as read-only semantic text without excluded actions or legacy claims', () => {
    const html = render(selected);
    assert.ok((html)?.includes('<h3 id="'));
    assert.match(html, /<h3 id="[^"]+" class="ot-relationship-selected-title">Correction 204 · version 3<\/h3>/);
    assert.ok((html)?.includes('<p class="ot-relationship-selected-note">Selected in graph and relationship table</p>'));
    assert.ok((html)?.includes('<dl class="ot-relationship-reference"><dt>Exact reference</dt>' +
      '<dd class="ot-relationship-reference-value">correction:204 · revision 3</dd>' +
      '<dd class="ot-relationship-reference-detail">Digest sha256: 6f83…d124</dd></dl>'));
    const visible = text(html);
    for (const excluded of ['Copy reference', 'Open local evidence', 'Lineage gaps', '0 on selected path', 'Current context']) {
      assert.ok((visible)?.includes(excluded) === false);
    }
    assert.doesNotMatch(html, interactive);
    assert.doesNotMatch(html, /role="(?:status|dialog)"|aria-live/);
  });

  it('replaces a selection with only the new details and clears all details when the selection is null', () => {
    const first = render(selected);
    const replaced = render({ view: 'selected', rows: selectedRows, selected: alternateContext });
    const cleared = render({ view: 'selected', rows: selectedRows, selected: null });
    for (const value of Object.values(selectedContext)) {
      if (value === alternateContext.note) continue;
      assert.ok((first)?.includes(value));
      assert.ok((replaced)?.includes(value) === false);
    }
    for (const value of Object.values(alternateContext)) assert.ok((replaced)?.includes(value));
    for (const value of [...Object.values(selectedContext), ...Object.values(alternateContext), 'Exact reference']) {
      assert.ok((cleared)?.includes(value) === false);
    }
    assert.ok((cleared)?.includes('ot-relationship-selected') === false);
    assert.deepEqual(table(cleared), table(first));
  });
});

describe('OPE-319 empty and redacted semantics', () => {
  it('renders the exact empty copy and native two-column row without claiming no dependencies or a zero', () => {
    const html = render({ view: 'empty' });
    const copy = relationshipCopy.empty;
    assert.match(html, /<h2 id="([^"]+)" class="ot-relationship-empty-title">No visible relationships<\/h2>/);
    assert.ok((html)?.includes('<p class="ot-relationship-empty-body">Nothing is currently visible in this authorized view. ' +
      'This does not establish that the item has no dependencies.</p>'));
    assert.ok((html)?.includes('<p class="ot-relationship-empty-total">Total relationships: Not available</p>'));
    const rows = table(html);
    assert.deepEqual(rows.map(row => row.map(cell => [cell.scope, cell.text])), [
      [['col', 'Relationship'], ['col', 'Current view']],
      [['row', 'No visible relationships'], [null, 'No permitted rows returned']],
    ]);
    assert.deepEqual(records(html), [['Relationship: No visible relationships', 'Current view: No permitted rows returned']]);
    const visible = text(html);
    assert.doesNotMatch(visible, /\d/);
    assert.doesNotMatch(visible, /has no dependencies(?!\.)|\bno relationships exist|\bnone exist/i);
    assert.doesNotMatch(visible.replace(copy.body, ''), /no dependencies/i);
    assert.doesNotMatch(html, interactive);
  });

  it('renders the approved Hold notice and only the permitted redaction placeholders', () => {
    const html = render({ view: 'redacted' });
    assert.ok((html)?.includes('<div role="status" class="ot-notice ot-notice--hold"><p class="ot-notice-title">' +
      'Some relationship details are restricted</p><p class="ot-notice-body">Your authorized view remains available. ' +
      'Restricted names, types and counts are not shown.</p></div>'));
    const rows = table(html);
    assert.deepEqual(rows.map(row => row.map(cell => [cell.scope, cell.text])), [
      [['col', 'Source'], ['col', 'Relationship'], ['col', 'Target'], ['col', 'Access']],
      [['row', 'Restricted item'], [null, 'Redacted relationship'], [null, 'Current visible item'], [null, 'Permission redacted']],
    ]);
    assert.deepEqual(records(html), [['Source: Restricted item', 'Relationship: Redacted relationship',
      'Target: Current visible item', 'Access: Permission redacted']]);
    assert.ok((html)?.includes('aria-label="Some relationship details are restricted"'));
    // The complete visible text is exactly the fixed public copy (table and narrow stack both present).
    const permitted = [relationshipCopy.redacted.title, relationshipCopy.redacted.body,
      ...relationshipCopy.redacted.columns, ...relationshipCopy.redacted.row,
      ...relationshipCopy.redacted.columns.map((column, index) => `${column}: ${relationshipCopy.redacted.row[index]}`)];
    let remaining = text(html);
    for (const value of permitted.sort((a, b) => b.length - a.length)) remaining = remaining.split(value).join('');
    assert.equal(remaining.trim(), '');
    assert.doesNotMatch(text(html), /\d/);
    const attributes = [...html.matchAll(/\s([\w-]+)="([^"]*)"/g)].map(match => [match[1], match[2]]);
    for (const [name, value] of attributes) {
      assert.ok((['class', 'role', 'scope', 'aria-label'])?.includes(name));
      if (name === 'aria-label') assert.equal(value, relationshipCopy.redacted.title);
    }
  });

  testCases([
    ['redacted', { view: 'redacted', [sentinel]: sentinel }],
    ['redacted', { view: 'redacted', hiddenName: sentinel, count: 7 }],
    ['redacted', { view: 'redacted', [sentinel]: { name: sentinel, total: 7 } }],
    ['empty', { view: 'empty', total: '0', [sentinel]: sentinel }],
    ['empty', { view: 'empty', body: `${sentinel} has no dependencies` }],
  ])('refuses data on the fixed %s state without echoing a sentinel key or value', (view, props) => {
    const { error, html } = attempt(props);
    assert.equal(error.message, `Undeclared ${view} view fields are refused.`);
    assert.ok((String(error.stack))?.includes(sentinel) === false);
    assert.equal(html, null);
  });
});

describe('OPE-319 closed, verbatim presentation interface', () => {
  it('preserves and escapes nonstandard caller strings exactly without formatting', () => {
    const odd = {
      source: '<img src=x onerror="alert(1)">', relation: '  spaced  ', target: '００１２ fullwidth',
      version: '3 → 5 → ?', validity: 'v3.10 &amp; unknown',
    };
    const html = render({ view: 'selected', rows: [{ key: 'odd', ...odd }], selected: {
      title: '<script>generated</script>', note: '0', reference: 'ref:"quoted"', referenceDetail: '6f83…d124',
    } });
    assert.doesNotMatch(html, /<img|<script/);
    assert.ok((html)?.includes('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;'));
    assert.ok((html)?.includes('&lt;script&gt;generated&lt;/script&gt;'));
    assert.deepEqual(table(html)[1].map(cell => cell.text), Object.values(odd));
    assert.deepEqual(records(html)[0], ['Source', 'Relation', 'Target', 'Version', 'Validity']
      .map((label, index) => `${label}: ${Object.values(odd)[index]}`));
    assert.ok((html)?.includes('<p class="ot-relationship-selected-note">0</p>'));
    const records16 = render({ view: 'records', observation: ' 006 records ', rows: [
      { key: 'x', node: '012', relationship: 'Not available', version: '1.50' }] });
    assert.deepEqual(table(records16)[1].map(cell => cell.text), ['012', 'Not available', '1.50']);
    assert.ok((records16)?.includes('<p class="ot-relationship-observation"> 006 records </p>'));
  });

  // Every rejection carries the generated sentinel as a caller key and/or value; none may echo it.
  const sentinelRow = { ...selectedRows[0], key: sentinel, source: sentinel };
  testCases([
    ['unknown view', { view: sentinel }],
    ['non-object rows', { view: 'selected', rows: sentinel, selected: null }],
    ['no rows', { view: 'selected', rows: [], selected: null, [sentinel]: sentinel }],
    ['records without rows', { view: 'records', rows: [], observation: sentinel }],
    ['undefined selection', { view: 'selected', rows: [sentinelRow] }],
    ['non-object selection', { view: 'selected', rows: [sentinelRow], selected: sentinel }],
    ['missing row field', { view: 'selected', rows: [{ key: sentinel, source: sentinel }], selected: null }],
    ['blank row text', { view: 'selected', rows: [{ ...sentinelRow, target: '  ' }], selected: null }],
    ['blank row key', { view: 'selected', rows: [{ ...sentinelRow, key: ' ' }], selected: null }],
    ['duplicate keys', { view: 'selected', rows: [sentinelRow, sentinelRow], selected: null }],
    ['undeclared row field', { view: 'selected', rows: [{ ...sentinelRow, [sentinel]: sentinel }], selected: null }],
    ['legacy lineage field', { view: 'selected', rows: [sentinelRow], selected: { ...selectedContext, lineageGaps: sentinel } }],
    ['undeclared context key', { view: 'selected', rows: [sentinelRow], selected: { ...selectedContext, [sentinel]: sentinel } }],
    ['blank context text', { view: 'selected', rows: [sentinelRow], selected: { ...selectedContext, title: '', note: sentinel } }],
    ['action callback', { view: 'selected', rows: selectedRows, selected: selectedContext, onCopy: () => sentinel }],
    ['mode prop', { view: 'records', rows: recordRows, observation: recordObservation, mode: sentinel }],
    ['undeclared tone', { view: 'records', rows: [{ ...recordRows[0], node: sentinel, versionTone: sentinel }], observation: sentinel }],
    ['undeclared record field', { view: 'records', rows: [{ ...recordRows[0], [sentinel]: sentinel }], observation: sentinel }],
    ['blank observation', { view: 'records', rows: [{ ...recordRows[0], node: sentinel }], observation: '' }],
  ])('refuses %s with a fixed diagnostic', (_name, props) => {
    const { error, html } = attempt(props);
    assert.ok((error.message)?.includes(sentinel) === false);
    assert.ok((error.message)?.includes('generated-restricted') === false);
    assert.ok((String(error.stack))?.includes(sentinel) === false);
    assert.equal(html, null);
  });
});
