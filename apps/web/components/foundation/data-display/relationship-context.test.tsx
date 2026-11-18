import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
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
    expect(caught).toBeInstanceOf(Error);
    return { error: caught as Error, html };
  }
  throw new Error('Expected the refused props to throw.');
}

const populated: RelationshipContextProps ={ view: 'records', rows: recordRows, observation: recordObservation };
const selected: RelationshipContextProps = { view: 'selected', rows: selectedRows, selected: selectedContext };

describe('OPE-319 read-only relationship records (16:81 data portion)', () => {
  it('renders the Details heading, native Node/Relationship/Version headers and all six exact rows', () => {
    const html = render(populated);
    expect(html).toMatch(/<h2 class="ot-relationship-section-title"><span id="[^"]+">Details<\/span><\/h2>/);
    const rows = table(html);
    expect(rows[0].map(cell => [cell.tag, cell.scope, cell.text])).toEqual([
      ['th', 'col', 'Node'], ['th', 'col', 'Relationship'], ['th', 'col', 'Version']]);
    expect(rows.slice(1).map(row => row.map(cell => cell.text))).toEqual([
      ['Policy v2', 'Defines requirement', '12'],
      ['Case refund-042', 'Violates requirement', '3'],
      ['Verified correction', 'Derived from evidence', '2'],
      ['Dataset ds-014', 'Includes correction', 'Sealed'],
      ['Evaluation eval-014', 'Tests requirement', 'Frozen'],
      ['Release r1', 'Held by retention', '1'],
    ]);
    for (const row of rows.slice(1)) expect(row.map(cell => [cell.tag, cell.scope])).toEqual([['th', 'row'], ['td', null], ['td', null]]);
    expect(html).toMatch(/<table class="ot-relationship-records-table" aria-labelledby="([^"]+)">/);
    const [, titleId] = /<span id="([^"]+)">Details<\/span>/.exec(html)!;
    expect(html).toContain(`aria-labelledby="${titleId}"`);
  });

  it('paints blue headers, a single mint cell for the caller-toned Sealed row and preserves the observation', () => {
    const html = render(populated);
    expect(html.match(/ot-table-cell-frame--blue/g)).toHaveLength(3);
    const mint = table(html).flat().filter(cell => cell.markup.includes('ot-table-cell-frame--mint'));
    expect(mint.map(cell => cell.text)).toEqual(['Sealed']);
    expect(html).toContain(`<p class="ot-relationship-observation">${recordObservation}</p>`);
  });

  it('excludes search, filter, sort and pagination and exposes no action or tab stop', () => {
    const visible = text(render(populated));
    for (const excluded of ['Search', 'Filter', 'Sort', 'Previous', 'Next']) expect(visible).not.toContain(excluded);
    expect(render(populated)).not.toMatch(interactive);
    // The horizontal-scroll region only becomes a labelled keyboard stop once it is measured as scrollable.
    expect(render(populated)).not.toContain('role="region"');
  });

  it('never infers a tone or meaning from the version text', () => {
    const html = render({ view: 'records', observation: 'generated', rows: [
      { key: 'a', node: 'A', relationship: 'r', version: 'Sealed' },
      { key: 'b', node: 'B', relationship: 'r', version: '12', versionTone: 'mint' },
    ] });
    const cells = table(html).flat().filter(cell => cell.markup.includes('frame--mint'));
    expect(cells.map(cell => cell.text)).toEqual(['12']);
  });
});

describe('OPE-319 selected relationship table (165:11224/11321/11818/12315)', () => {
  it('associates the five native headers with the three exact rows on wider containers', () => {
    const html = render(selected);
    expect(html).toMatch(/<h2 id="([^"]+)" class="ot-relationship-section-title">Selected relationship<\/h2>/);
    const [, headingId] = /<h2 id="([^"]+)"/.exec(html)!;
    expect(html).toContain(`<table class="ot-relationship-table" aria-labelledby="${headingId}">`);
    const rows = table(html);
    expect(rows[0].map(cell => [cell.tag, cell.scope, cell.text])).toEqual(
      ['Source', 'Relation', 'Target', 'Version', 'Validity'].map(name => ['th', 'col', name]));
    expect(rows.slice(1).map(row => row.map(cell => cell.text))).toEqual([
      ['Correction 204', 'included in', 'Dataset 5', '3 → 5', 'Selected path'],
      ['Dataset 5', 'evaluated in', 'Evaluation 41', '5 → 41', 'Affected'],
      ['Unknown dependency', 'not reported', 'Release 4', 'Not available', 'Unknown'],
    ]);
    for (const row of rows.slice(1)) expect(row.map(cell => cell.scope)).toEqual(['row', null, null, null, null]);
  });

  it('provides the same records as labelled narrow stacks with five visible prefixes each', () => {
    const html = render(selected);
    const [, headingId] = /<h2 id="([^"]+)"/.exec(html)!;
    expect(html).toContain(`<ul class="ot-relationship-stack" aria-labelledby="${headingId}">`);
    expect(records(html)).toEqual([
      ['Source: Correction 204', 'Relation: included in', 'Target: Dataset 5', 'Version: 3 → 5', 'Validity: Selected path'],
      ['Source: Dataset 5', 'Relation: evaluated in', 'Target: Evaluation 41', 'Version: 5 → 41', 'Validity: Affected'],
      ['Source: Unknown dependency', 'Relation: not reported', 'Target: Release 4', 'Version: Not available', 'Validity: Unknown'],
    ]);
    expect(html.match(/<dt>/g)).toHaveLength(15 + 1);
    expect(html).toContain('3 → 5');
    expect(html).not.toMatch(/3 -&gt; 5|3->5/);
  });

  it('renders the exact selected-node context as read-only semantic text without excluded actions or legacy claims', () => {
    const html = render(selected);
    expect(html).toContain('<h3 id="');
    expect(html).toMatch(/<h3 id="[^"]+" class="ot-relationship-selected-title">Correction 204 · version 3<\/h3>/);
    expect(html).toContain('<p class="ot-relationship-selected-note">Selected in graph and relationship table</p>');
    expect(html).toContain('<dl class="ot-relationship-reference"><dt>Exact reference</dt>' +
      '<dd class="ot-relationship-reference-value">correction:204 · revision 3</dd>' +
      '<dd class="ot-relationship-reference-detail">Digest sha256: 6f83…d124</dd></dl>');
    const visible = text(html);
    for (const excluded of ['Copy reference', 'Open local evidence', 'Lineage gaps', '0 on selected path', 'Current context']) {
      expect(visible).not.toContain(excluded);
    }
    expect(html).not.toMatch(interactive);
    expect(html).not.toMatch(/role="(?:status|dialog)"|aria-live/);
  });

  it('replaces a selection with only the new details and clears all details when the selection is null', () => {
    const first = render(selected);
    const replaced = render({ view: 'selected', rows: selectedRows, selected: alternateContext });
    const cleared = render({ view: 'selected', rows: selectedRows, selected: null });
    for (const value of Object.values(selectedContext)) {
      if (value === alternateContext.note) continue;
      expect(first).toContain(value);
      expect(replaced).not.toContain(value);
    }
    for (const value of Object.values(alternateContext)) expect(replaced).toContain(value);
    for (const value of [...Object.values(selectedContext), ...Object.values(alternateContext), 'Exact reference']) {
      expect(cleared).not.toContain(value);
    }
    expect(cleared).not.toContain('ot-relationship-selected');
    expect(table(cleared)).toEqual(table(first));
  });
});

describe('OPE-319 empty and redacted semantics', () => {
  it('renders the exact empty copy and native two-column row without claiming no dependencies or a zero', () => {
    const html = render({ view: 'empty' });
    const copy = relationshipCopy.empty;
    expect(html).toMatch(/<h2 id="([^"]+)" class="ot-relationship-empty-title">No visible relationships<\/h2>/);
    expect(html).toContain('<p class="ot-relationship-empty-body">Nothing is currently visible in this authorized view. ' +
      'This does not establish that the item has no dependencies.</p>');
    expect(html).toContain('<p class="ot-relationship-empty-total">Total relationships: Not available</p>');
    const rows = table(html);
    expect(rows.map(row => row.map(cell => [cell.scope, cell.text]))).toEqual([
      [['col', 'Relationship'], ['col', 'Current view']],
      [['row', 'No visible relationships'], [null, 'No permitted rows returned']],
    ]);
    expect(records(html)).toEqual([['Relationship: No visible relationships', 'Current view: No permitted rows returned']]);
    const visible = text(html);
    expect(visible).not.toMatch(/\d/);
    expect(visible).not.toMatch(/has no dependencies(?!\.)|\bno relationships exist|\bnone exist/i);
    expect(visible.replace(copy.body, '')).not.toMatch(/no dependencies/i);
    expect(html).not.toMatch(interactive);
  });

  it('renders the approved Hold notice and only the permitted redaction placeholders', () => {
    const html = render({ view: 'redacted' });
    expect(html).toContain('<div role="status" class="ot-notice ot-notice--hold"><p class="ot-notice-title">' +
      'Some relationship details are restricted</p><p class="ot-notice-body">Your authorized view remains available. ' +
      'Restricted names, types and counts are not shown.</p></div>');
    const rows = table(html);
    expect(rows.map(row => row.map(cell => [cell.scope, cell.text]))).toEqual([
      [['col', 'Source'], ['col', 'Relationship'], ['col', 'Target'], ['col', 'Access']],
      [['row', 'Restricted item'], [null, 'Redacted relationship'], [null, 'Current visible item'], [null, 'Permission redacted']],
    ]);
    expect(records(html)).toEqual([['Source: Restricted item', 'Relationship: Redacted relationship',
      'Target: Current visible item', 'Access: Permission redacted']]);
    expect(html).toContain('aria-label="Some relationship details are restricted"');
    // The complete visible text is exactly the fixed public copy (table and narrow stack both present).
    const permitted = [relationshipCopy.redacted.title, relationshipCopy.redacted.body,
      ...relationshipCopy.redacted.columns, ...relationshipCopy.redacted.row,
      ...relationshipCopy.redacted.columns.map((column, index) => `${column}: ${relationshipCopy.redacted.row[index]}`)];
    let remaining = text(html);
    for (const value of permitted.sort((a, b) => b.length - a.length)) remaining = remaining.split(value).join('');
    expect(remaining.trim()).toBe('');
    expect(text(html)).not.toMatch(/\d/);
    const attributes = [...html.matchAll(/\s([\w-]+)="([^"]*)"/g)].map(match => [match[1], match[2]]);
    for (const [name, value] of attributes) {
      expect(['class', 'role', 'scope', 'aria-label']).toContain(name);
      if (name === 'aria-label') expect(value).toBe(relationshipCopy.redacted.title);
    }
  });

  it.each([
    ['redacted', { view: 'redacted', [sentinel]: sentinel }],
    ['redacted', { view: 'redacted', hiddenName: sentinel, count: 7 }],
    ['redacted', { view: 'redacted', [sentinel]: { name: sentinel, total: 7 } }],
    ['empty', { view: 'empty', total: '0', [sentinel]: sentinel }],
    ['empty', { view: 'empty', body: `${sentinel} has no dependencies` }],
  ])('refuses data on the fixed %s state without echoing a sentinel key or value', (view, props) => {
    const { error, html } = attempt(props);
    expect(error.message).toBe(`Undeclared ${view} view fields are refused.`);
    expect(String(error.stack)).not.toContain(sentinel);
    expect(html).toBeNull();
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
    expect(html).not.toMatch(/<img|<script/);
    expect(html).toContain('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
    expect(html).toContain('&lt;script&gt;generated&lt;/script&gt;');
    expect(table(html)[1].map(cell => cell.text)).toEqual(Object.values(odd));
    expect(records(html)[0]).toEqual(['Source', 'Relation', 'Target', 'Version', 'Validity']
      .map((label, index) => `${label}: ${Object.values(odd)[index]}`));
    expect(html).toContain('<p class="ot-relationship-selected-note">0</p>');
    const records16 = render({ view: 'records', observation: ' 006 records ', rows: [
      { key: 'x', node: '012', relationship: 'Not available', version: '1.50' }] });
    expect(table(records16)[1].map(cell => cell.text)).toEqual(['012', 'Not available', '1.50']);
    expect(records16).toContain('<p class="ot-relationship-observation"> 006 records </p>');
  });

  // Every rejection carries the generated sentinel as a caller key and/or value; none may echo it.
  const sentinelRow = { ...selectedRows[0], key: sentinel, source: sentinel };
  it.each([
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
    expect(error.message).not.toContain(sentinel);
    expect(error.message).not.toContain('generated-restricted');
    expect(String(error.stack)).not.toContain(sentinel);
    expect(html).toBeNull();
  });
});
