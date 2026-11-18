import { expect, test, type Page, type TestInfo } from '@playwright/test';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { build } from 'vite';
import type { RelationshipFixture } from '../../../tests/fixtures/presentation/relationship-context';

declare global { interface Window { showRelationshipFixture(name: RelationshipFixture): void } }

/** Generated local-only gallery. Every other request is aborted and recorded as a failure. */
const origin = 'http://relationship.test';
let script = '';
let styles = '';
test.beforeAll(async () => {
  const root = join(process.cwd(), 'apps/web');
  const bundle = await build({
    root, configFile: false, envFile: false, publicDir: false, logLevel: 'error',
    oxc: { jsx: { runtime: 'automatic' } },
    build: {
      write: false, assetsInlineLimit: 1024 * 1024,
      lib: { entry: join(root, 'tests/fixtures/presentation/relationship-context.tsx'), formats: ['es'] },
    },
    define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  });
  const output = Array.isArray(bundle) ? bundle[0] : bundle;
  if (!('output' in output)) throw new Error('Missing relationship gallery output');
  const js = output.output.find(file => file.type === 'chunk');
  const css = output.output.find(file => file.type === 'asset' && file.fileName.endsWith('.css'));
  if (!js || js.type !== 'chunk' || !css || css.type !== 'asset') throw new Error('Missing relationship gallery assets');
  script = js.code;
  styles = String(css.source);
  if (!styles.includes('data:font/woff2') && !styles.includes('data:application/font-woff2')) throw new Error('Geist was not inlined');
});

const states = ['populated', 'selected', 'empty', 'redacted'] as const;
const padding = (width: number) => width === 320 ? 16 : width === 768 ? 24 : 32;

async function open(page: Page, width: number, scale: number) {
  const requests: string[] = [];
  // Browser-initiated favicon probes are not gallery requests; every other request is recorded.
  page.on('request', request => {
    const url = request.url();
    if (!url.startsWith('data:') && url !== origin + '/favicon.ico') requests.push(url);
  });
  await page.route('**/*', route => {
    const url = route.request().url();
    if (url === origin + '/') {
      return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
        '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Relationship context</title><style>' +
        styles + '</style></head><body><div id="root"></div><script type="module" src="/gallery.js"></script></body></html>' });
    }
    if (url === origin + '/gallery.js') return route.fulfill({ contentType: 'text/javascript', body: script });
    return route.abort();
  });
  await page.setViewportSize({ width, height: 900 });
  await page.goto(origin + '/');
  await page.waitForFunction(() => typeof window.showRelationshipFixture === 'function');
  await page.evaluate(scale => { document.documentElement.style.fontSize = `${scale}%`; }, scale);
  return requests;
}

async function show(page: Page, name: RelationshipFixture) {
  await page.evaluate(name => window.showRelationshipFixture(name), name);
  await page.evaluate(async () => {
    await document.fonts.load('400 14px Geist'); await document.fonts.load('500 14px Geist');
    await document.fonts.load('600 18px Geist'); await document.fonts.load('600 24px Geist'); await document.fonts.ready;
  });
  expect(await page.evaluate(() => [...document.fonts].some(face => face.family === 'Geist' && face.status === 'loaded'))).toBe(true);
  // One frame for ResizeObserver-driven scroll measurement to settle.
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

/** Rendered dimensions, clipping and ARIA tree, attached as JSON for every capture. */
async function record(page: Page, info: TestInfo, name: string) {
  const geometry = await page.evaluate(() => {
    const box = (node: Element) => { const b = node.getBoundingClientRect(); return { x: b.x, y: b.y, width: b.width, height: b.height }; };
    const visible = (node: Element) => node.checkVisibility();
    const boxes: Record<string, ReturnType<typeof box>[]> = {};
    for (const selector of ['.ot-relationship-context', '.ot-relationship-records', '.ot-relationship-scroll', '.ot-relationship-records-table',
      '.ot-relationship-grid', '.ot-relationship-table', '.ot-relationship-stack', '.ot-relationship-record', '.ot-relationship-selected',
      '.ot-relationship-reference', '.ot-relationship-empty', '.ot-notice', '.relationship-gallery-rail']) {
      boxes[selector] = [...document.querySelectorAll(selector)].filter(visible).map(box);
    }
    const clipped = [...document.querySelectorAll('#root :is(th, td, p, h2, h3, li, dt, dd, .ot-relationship-line, .ot-table-cell-frame)')]
      .filter(node => visible(node) && getComputedStyle(node).display !== 'inline' && node.scrollWidth > node.clientWidth + 1)
      .map(node => node.textContent);
    const headerTextWidths = [...document.querySelectorAll('.ot-relationship-table thead th')].filter(visible).map(node => {
      const style = getComputedStyle(node);
      return node.getBoundingClientRect().width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    });
    const recordHeaderFrames = [...document.querySelectorAll('.ot-relationship-records-table thead .ot-table-cell-frame')].map(node => box(node).width);
    // Native border convention: start inset from the frame's outer edge, end remainder and child width.
    const inset = (frame: Element | null | undefined, child: Element | null | undefined) => {
      if (!frame || !child || !visible(frame) || !visible(child)) return null;
      const outer = frame.getBoundingClientRect(); const inner = child.getBoundingClientRect();
      return { start: inner.x - outer.x, end: outer.right - inner.right, width: inner.width };
    };
    /** Text box of a padded cell (content box), so column text starts and widths can be compared. */
    const textBox = (cell: Element) => {
      const bounds = cell.getBoundingClientRect(); const style = getComputedStyle(cell);
      return { x: bounds.x + parseFloat(style.paddingLeft), right: bounds.right - parseFloat(style.paddingRight) };
    };
    const gap = (above: Element | null | undefined, below: Element | null | undefined) =>
      above && below && visible(above) && visible(below) ? below.getBoundingClientRect().y - above.getBoundingClientRect().bottom : null;
    const grid = [...document.querySelectorAll('.ot-relationship-grid')].find(visible);
    const headers = grid ? [...grid.querySelectorAll('.ot-relationship-table thead th')].filter(visible).map(textBox) : [];
    const outerGrid = grid?.getBoundingClientRect();
    const records = document.querySelector('.ot-relationship-records');
    const selected = document.querySelector('.ot-relationship-selected');
    const empty = document.querySelector('.ot-relationship-empty');
    const native = {
      records: inset(records, records?.querySelector('.ot-relationship-scroll')),
      recordsHeading: inset(records, records?.querySelector('h2')),
      gridHeaderText: outerGrid && headers.length ? {
        start: headers[0].x - outerGrid.x, end: outerGrid.right - headers.at(-1)!.right,
        steps: headers.slice(1).map((header, index) => header.x - headers[index].x),
      } : null,
      stackLines: grid ? [...grid.querySelectorAll('.ot-relationship-record')].filter(visible)
        .map(item => inset(grid, item.querySelector('.ot-relationship-line'))) : [],
      context: inset(selected, selected?.querySelector('.ot-relationship-reference')),
      contextTitle: inset(selected, selected?.querySelector('h3')),
      reference: inset(selected?.querySelector('.ot-relationship-reference'), selected?.querySelector('.ot-relationship-reference dt')),
      empty: inset(empty, empty?.querySelector('h2')),
      gaps: {
        headingToTable: gap(document.querySelector('.ot-relationship-context--selected > h2'), grid),
        tableToContext: gap(grid, selected),
        emptyToTable: gap(empty, grid),
        noticeToTable: gap(document.querySelector('.ot-notice'), grid),
      },
    };
    const scroll = document.querySelector('.ot-relationship-scroll');
    return {
      viewport: { width: innerWidth, height: innerHeight }, rootFontSize: getComputedStyle(document.documentElement).fontSize,
      documentScrollWidth: document.documentElement.scrollWidth, boxes, clipped, headerTextWidths, recordHeaderFrames, native,
      visibleText: (document.querySelector('#root') as HTMLElement).innerText,
      recordsScroll: scroll ? { scrollWidth: scroll.scrollWidth, clientWidth: scroll.clientWidth, role: scroll.getAttribute('role'), tabIndex: scroll.getAttribute('tabindex') } : null,
    };
  });
  const aria = await page.locator('#root').ariaSnapshot();
  await info.attach(`geometry-${name}.json`, { body: JSON.stringify({ ...geometry, aria }, null, 2), contentType: 'application/json' });
  return { ...geometry, aria };
}

async function capture(page: Page, info: TestInfo, name: string) {
  const shot = await page.screenshot({ fullPage: true, animations: 'disabled', caret: 'hide' });
  await writeFile(info.outputPath(`${name}.png`), shot);
}

/**
 * Strings the accessibility tree exposes to a user: accessible names and text. Role metadata such
 * as `[level=2]` is structure, not exposed copy, and is excluded before any count check.
 */
function exposedAria(aria: string) {
  const values: string[] = [];
  for (const raw of aria.split('\n')) {
    const line = raw.replace(/^\s*-\s*/, '').replace(/\s*\[[^\]]*\]/g, '');
    const named = /^[\w-]+ "((?:[^"\\]|\\.)*)"/.exec(line);
    if (named) values.push(named[1]);
    const text = /^[\w-]+(?: "(?:[^"\\]|\\.)*")?: (.+)$/.exec(line);
    if (text) values.push(text[1].replace(/^"(.*)"$/, '$1'));
  }
  return values;
}
/** What remains of exposed strings once every permitted fixed fragment is removed (longest first). */
function residue(values: readonly string[], permitted: readonly string[]) {
  const ordered = [...permitted].sort((a, b) => b.length - a.length);
  return values.map(value => ordered.reduce((rest, fragment) => rest.split(fragment).join(''), value).trim()).filter(Boolean);
}

const noActions ='button, a[href], input, select, textarea, dialog, [contenteditable], [aria-live], [role=alert], [role=dialog]';

for (const width of [320, 768, 1280, 1440]) for (const scale of [100, 200]) {
  test(`relationship states at ${width}px and ${scale}% text`, async ({ page }, info) => {
    const requests = await open(page, width, scale);
    const content = width - 2 * padding(width);
    const selectedContent = width >= 1280 ? content - 240 : content;
    const stacked = width === 320 || (scale === 200 && width === 768);
    for (const state of states) {
      await show(page, state);
      const name = `relationship-${state}-${width}-${scale}`;
      const geometry = await record(page, info, name);
      expect(geometry.documentScrollWidth).toBe(width);
      expect(geometry.rootFontSize).toBe(`${16 * scale / 100}px`);
      expect(geometry.clipped).toEqual([]);
      await expect(page.locator(noActions)).toHaveCount(0);
      const tables = page.getByRole('table');
      if (state === 'populated') {
        const table = page.getByRole('table', { name: 'Details', exact: true });
        await expect(page.getByRole('heading', { level: 2, name: /^Details/ })).toBeVisible();
        await expect(table.getByRole('columnheader')).toHaveText(['Node', 'Relationship', 'Version']);
        await expect(table.getByRole('rowheader')).toHaveText(['Policy v2', 'Case refund-042', 'Verified correction',
          'Dataset ds-014', 'Evaluation eval-014', 'Release r1']);
        await expect(table.getByRole('row', { name: 'Dataset ds-014 Includes correction Sealed', exact: true })).toBeAttached();
        await expect(table.getByRole('cell')).toHaveText(['Defines requirement', '12', 'Violates requirement', '3', 'Derived from evidence', '2',
          'Includes correction', 'Sealed', 'Tests requirement', 'Frozen', 'Held by retention', '1']);
        await expect(page.getByText('6 records · observed 09:42 UTC', { exact: true })).toBeVisible();
        for (const absent of ['Search records', 'Filter', 'Sort', 'Previous', 'Next']) await expect(page.getByText(absent, { exact: true })).toHaveCount(0);
        await expect(table.getByRole('cell', { name: 'Sealed' }).locator('.ot-table-cell-frame')).toHaveCSS('background-color', 'rgb(223, 245, 235)');
        await expect(table.getByRole('columnheader', { name: 'Node' }).locator('.ot-table-cell-frame')).toHaveCSS('background-color', 'rgb(237, 243, 255)');
        const card = geometry.boxes['.ot-relationship-records'][0];
        expect(card.width).toBe(Math.min(792, content));
        if (width === 320) {
          const region = page.getByRole('region', { name: 'Details — scroll horizontally', exact: true });
          await expect(region).toBeVisible();
          expect(geometry.recordsScroll!.scrollWidth).toBeGreaterThan(geometry.recordsScroll!.clientWidth);
          await page.keyboard.press('Tab');
          await expect(region).toBeFocused();
          await expect(region).toHaveCSS('outline-style', 'solid');
          await page.keyboard.press('ArrowRight');
          await expect.poll(() => region.evaluate(node => node.scrollLeft)).toBeGreaterThan(0);
          await region.evaluate(node => { node.scrollLeft = 0; });
        } else {
          await expect(page.getByRole('region')).toHaveCount(0);
          expect(geometry.recordsScroll!.tabIndex).toBeNull();
          await page.keyboard.press('Tab');
          expect(await page.evaluate(() => document.activeElement === document.body || document.activeElement === null)).toBe(true);
        }
        // 16:81: the 752 table starts 21px into the 792 card with a 19px remainder; narrower cards keep the insets.
        expect(geometry.native.records).toEqual({ start: 21, end: 19, width: card.width - 40 });
        expect(geometry.native.recordsHeading).toEqual({ start: 21, end: 19, width: card.width - 40 });
        if (scale === 100 && width >= 768) {
          // 1 + 20 + Details 26 + 16 + table 418 (46 header + 6 × (16 + 46)) + 16 + observation 18 + 20 + 1.
          expect(card.height).toBe(536);
          expect(geometry.boxes['.ot-relationship-records-table'][0].height).toBe(418);
        }
        if (scale === 100 && width >= 1280) {
          expect(geometry.boxes['.ot-relationship-records-table'][0].width).toBe(752);
          for (const [index, native] of [288, 272, 188].entries()) expect(geometry.recordHeaderFrames[index]).toBeCloseTo(native, 1);
        }
      } else {
        const heading = state === 'selected' ? 'Selected relationship' : state === 'empty' ? 'No visible relationships' : 'Some relationship details are restricted';
        const columns = state === 'selected' ? ['Source', 'Relation', 'Target', 'Version', 'Validity']
          : state === 'empty' ? ['Relationship', 'Current view'] : ['Source', 'Relationship', 'Target', 'Access'];
        const rows = state === 'selected' ? [
          ['Correction 204', 'included in', 'Dataset 5', '3 → 5', 'Selected path'],
          ['Dataset 5', 'evaluated in', 'Evaluation 41', '5 → 41', 'Affected'],
          ['Unknown dependency', 'not reported', 'Release 4', 'Not available', 'Unknown'],
        ] : state === 'empty' ? [['No visible relationships', 'No permitted rows returned']]
          : [['Restricted item', 'Redacted relationship', 'Current visible item', 'Permission redacted']];
        if (stacked) {
          await expect(tables).toHaveCount(0);
          const list = page.getByRole('list', { name: heading, exact: true });
          await expect(list.getByRole('listitem')).toHaveCount(rows.length);
          for (const [index, row] of rows.entries()) {
            const item = list.getByRole('listitem').nth(index);
            await expect(item.locator('.ot-relationship-line')).toHaveText(columns.map((column, cell) => `${column}: ${row[cell]}`));
            await expect(item.locator('dt')).toHaveText(columns.map(column => `${column}:`));
          }
        } else {
          await expect(page.getByRole('list')).toHaveCount(0);
          const table = page.getByRole('table', { name: heading, exact: true });
          await expect(table.getByRole('columnheader')).toHaveText(columns);
          await expect(table.getByRole('rowheader')).toHaveText(rows.map(row => row[0]));
          await expect(table.getByRole('cell')).toHaveText(rows.flatMap(row => row.slice(1)));
          await expect(table.getByRole('row').nth(1).getByRole('rowheader')).toHaveCSS('font-weight', '500');
          await expect(table.getByRole('row').nth(1).getByRole('cell').first()).toHaveCSS('font-weight', '400');
        }
        // Native: equal text columns of 128 at 720 and 211.2 at 1136 (five), 338 at 720 (two), stepping by +12.
        const outer = state === 'selected' ? selectedContent : content;
        const textColumn = (outer - 32 - 12 * (columns.length - 1)) / columns.length;
        if (stacked) {
          // Native 288 records: 256px text from 17px (first) and 18px (each bordered later record).
          expect(geometry.native.stackLines.map(line => line!.start)).toEqual(rows.map((_, index) => index === 0 ? 17 : 18));
          for (const line of geometry.native.stackLines) expect(line!.width).toBe(outer - 32);
        } else {
          const text = geometry.headerTextWidths;
          expect(Math.max(...text) - Math.min(...text)).toBeLessThan(0.05);
          expect(text[0]).toBeCloseTo(textColumn, 1);
          const header = geometry.native.gridHeaderText!;
          expect(header.start).toBeCloseTo(17, 1);
          expect(header.end).toBeCloseTo(15, 1);
          expect(header.steps).toHaveLength(columns.length - 1);
          for (const step of header.steps) expect(step).toBeCloseTo(textColumn + 12, 1);
        }
        const grid = geometry.boxes['.ot-relationship-grid'][0];
        expect(grid.width).toBe(state === 'selected' ? selectedContent : content);
        if (state === 'selected') {
          if (scale === 100) expect(grid.height).toBe(width === 320 ? 522 : width === 768 ? 234 : 216);
          if (!stacked) {
            const fills = await page.locator('.ot-relationship-table tr > :first-child').evaluateAll(cells => cells.map(cell => getComputedStyle(cell).backgroundColor));
            expect(fills).toEqual(['rgb(250, 251, 253)', 'rgb(255, 255, 255)', 'rgb(250, 251, 253)', 'rgb(255, 255, 255)']);
          }
          const details = page.getByRole('group', { name: 'Correction 204 · version 3', exact: true });
          await expect(details.getByRole('heading', { level: 3 })).toHaveText('Correction 204 · version 3');
          await expect(details.getByText('Selected in graph and relationship table', { exact: true })).toBeVisible();
          await expect(details.getByRole('term')).toHaveText('Exact reference');
          await expect(details.getByRole('definition')).toHaveText(['correction:204 · revision 3', 'Digest sha256: 6f83…d124']);
          for (const excluded of ['Copy reference', 'Open local evidence', 'Lineage gaps', '0 on selected path']) {
            await expect(page.getByText(excluded)).toHaveCount(0);
          }
          const context = geometry.boxes['.ot-relationship-selected'][0];
          expect(context.width).toBe(Math.min(576, selectedContent));
          // 165:11243 family: field 544 at 576 (256 at 288) from 17px with a 15px remainder; its text likewise.
          expect(geometry.native.context).toEqual({ start: 17, end: 15, width: context.width - 32 });
          expect(geometry.native.contextTitle).toEqual({ start: 17, end: 15, width: context.width - 32 });
          expect(geometry.native.reference).toEqual({ start: 17, end: 15, width: context.width - 64 });
          // 1 + 16 + title 26 + 12 + note 18 + 12 + reference 108 (1 + 16 + 20 + 8 + 20 + 8 + 18 + 16 + 1) + 16 + 1.
          // The native 322px frame minus its excluded actions (12 + 44 + 12 + 44) is the same 210.
          if (scale === 100) expect(context.height).toBe(210);
          // Current parent boards: 20px heading-to-table and table-to-details gaps.
          expect(geometry.native.gaps.headingToTable).toBe(20);
          expect(geometry.native.gaps.tableToContext).toBe(20);
          expect(geometry.boxes['.relationship-gallery-rail'].map(rail => rail.width)).toEqual(width >= 1280 ? [240] : []);
        } else if (state === 'empty') {
          await expect(page.getByRole('heading', { level: 2, name: heading })).toBeVisible();
          await expect(page.getByText('Nothing is currently visible in this authorized view. This does not establish that the item has no dependencies.', { exact: true })).toBeVisible();
          await expect(page.getByText('Total relationships: Not available', { exact: true })).toBeVisible();
          expect(geometry.boxes['.ot-relationship-empty'][0].width).toBe(content);
          // 184:14 family: text 240 at 288 starts 25px with a 23px remainder; then 16px to the table.
          expect(geometry.native.empty).toEqual({ start: 25, end: 23, width: content - 48 });
          expect(geometry.native.gaps.emptyToTable).toBe(16);
          if (scale === 100 && !stacked) expect(grid.height).toBe(108);
          // The native heading stays a level-2 heading; that role metadata is structure, not a count.
          expect(geometry.aria).toContain(`heading "${heading}" [level=2]`);
          await expect(page.getByRole(stacked ? 'list' : 'table', { name: heading, exact: true })).toHaveAccessibleName(heading);
          const exposed = [...exposedAria(geometry.aria), geometry.visibleText];
          expect(exposed.filter(value => /\d/.test(value))).toEqual([]);
          expect(residue(exposed, ['No visible relationships', 'Total relationships: Not available',
            'Nothing is currently visible in this authorized view. This does not establish that the item has no dependencies.',
            ...columns.map(column => `${column}:`), ...columns, ...rows[0]])).toEqual([]);
        } else {
          const notice = page.getByRole('status');
          await expect(notice).toHaveText('Some relationship details are restrictedYour authorized view remains available. Restricted names, types and counts are not shown.');
          await expect(notice).toHaveCSS('background-color', 'rgb(255, 242, 213)');
          if (scale === 100 && !stacked) expect(grid.height).toBe(108);
          expect(geometry.native.gaps.noticeToTable).toBe(16);
          await expect(page.getByRole(stacked ? 'list' : 'table', { name: heading, exact: true })).toHaveAccessibleName(heading);
          // Exposed names and text carry only the fixed placeholders: no digits, names, types or counts.
          const exposedCopy = [...exposedAria(geometry.aria), geometry.visibleText];
          expect(exposedCopy.filter(value => /\d/.test(value))).toEqual([]);
          expect(residue(exposedCopy, ['Some relationship details are restricted',
            'Your authorized view remains available. Restricted names, types and counts are not shown.',
            ...columns.map(column => `${column}:`), ...columns, ...rows[0]])).toEqual([]);
          // Only the fixed public copy exists anywhere in the rendered DOM, including attribute values.
          const exposed = await page.locator('#root').evaluate(root => {
            const values: string[] = [];
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
            for (let node: Node | null = walker.currentNode; node; node = walker.nextNode()) {
              if (node.nodeType === Node.TEXT_NODE) { if (node.textContent!.trim()) values.push(node.textContent!); continue; }
              for (const attribute of (node as Element).attributes) if (attribute.name !== 'class') values.push(`${attribute.name}=${attribute.value}`);
            }
            return values;
          });
          const permitted = new Set(['Some relationship details are restricted', 'Your authorized view remains available. Restricted names, types and counts are not shown.',
            'Source', 'Relationship', 'Target', 'Access', 'Source:', 'Relationship:', 'Target:', 'Access:', ...rows[0],
            'role=status', 'scope=col', 'scope=row', 'aria-label=Some relationship details are restricted', 'id=root', 'aria-hidden=true']);
          expect(exposed.filter(value => !permitted.has(value))).toEqual([]);
        }
      }
      await capture(page, info, name);
    }
    expect([...new Set(requests)].sort()).toEqual([origin + '/', origin + '/gallery.js']);
    expect(await page.evaluate(() => localStorage.length + sessionStorage.length + document.cookie.length)).toBe(0);
  });
}

test('selected details are replaced and cleared on the same rerender without focus or announcements', async ({ page }, info) => {
  await open(page, 1280, 100);
  await show(page, 'selected');
  await page.locator('.ot-relationship-table').evaluate(node => { (node as HTMLElement & { generatedMark?: string }).generatedMark = 'same-table'; });
  const replaced = await page.evaluate(() => {
    window.showRelationshipFixture('selected-alternate');
    return document.querySelector('.ot-relationship-selected')?.textContent ?? null;
  });
  expect(replaced).toBe('Dataset 5 · version 5Generated alternate selectionExact referencedataset:5 · revision 5Digest sha256: generated-alternate');
  const cleared = await page.evaluate(() => {
    window.showRelationshipFixture('selected-cleared');
    return { details: document.querySelectorAll('.ot-relationship-selected').length, text: document.querySelector('#root')!.textContent,
      sameTable: (document.querySelector('.ot-relationship-table') as HTMLElement & { generatedMark?: string }).generatedMark === 'same-table',
      focus: document.activeElement === document.body };
  });
  expect(cleared.details).toBe(0);
  expect(cleared.sameTable).toBe(true);
  expect(cleared.focus).toBe(true);
  for (const old of ['Correction 204 · version 3', 'Dataset 5 · version 5', 'Exact reference', 'correction:204', 'dataset:5', 'Digest sha256']) {
    expect(cleared.text).not.toContain(old);
  }
  await expect(page.getByRole('table', { name: 'Selected relationship' }).getByRole('rowheader')).toHaveText(['Correction 204', 'Dataset 5', 'Unknown dependency']);
  await expect(page.locator('[aria-live], [role=status], [role=alert], [role=dialog], dialog, [aria-modal]')).toHaveCount(0);
  await record(page, info, 'relationship-selected-cleared');
  await capture(page, info, 'relationship-selected-cleared');
});

test('long unbroken and markup-like caller text wraps verbatim without page overflow', async ({ page }, info) => {
  for (const width of [320, 1440]) for (const scale of [100, 200]) {
    await open(page, width, scale);
    await show(page, 'long');
    const name = `relationship-long-${width}-${scale}`;
    const geometry = await record(page, info, name);
    expect(geometry.documentScrollWidth).toBe(width);
    expect(geometry.clipped).toEqual([]);
    await expect(page.locator('#root em, #root b')).toHaveCount(0);
    await expect(page.getByRole('cell', { name: '<em>caller text is not markup</em>', exact: true })).toBeAttached();
    // The inactive responsive representation is display:none, so exactly one visible match remains.
    await expect(page.getByText('3 → 5 → 8 → 13', { exact: true }).filter({ visible: true })).toHaveCount(1);
    const unbroken = 'Source-generated-unbroken'.repeat(6);
    await expect(page.getByText(unbroken, { exact: true }).filter({ visible: true })).toHaveCount(1);
    if (width === 320) await expect(page.getByText(`Source: ${unbroken}`, { exact: true })).toBeVisible();
    for (const box of [...geometry.boxes['.ot-relationship-grid'], ...geometry.boxes['.ot-relationship-selected'], ...geometry.boxes['.ot-relationship-records']]) {
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(width);
    }
    if (width === 320) {
      const region = page.getByRole('region', { name: 'Details — scroll horizontally', exact: true });
      await page.keyboard.press('Tab');
      await expect(region).toBeFocused();
    }
    await capture(page, info, name);
  }
});

test('forced colors keep boundaries, readable text and visible focus', async ({ page }, info) => {
  await page.emulateMedia({ forcedColors: 'active' });
  for (const width of [320, 1280]) {
    await open(page, width, 100);
    for (const state of states) {
      await show(page, state);
      const name = `relationship-forced-colors-${state}-${width}`;
      const geometry = await record(page, info, name);
      expect(geometry.documentScrollWidth).toBe(width);
      if (state === 'populated') {
        await expect(page.locator('.ot-relationship-records')).toHaveCSS('border-top-style', 'solid');
        await expect(page.locator('.ot-relationship-records-table .ot-table-cell-frame').first()).toHaveCSS('outline-style', 'solid');
        await expect(page.getByRole('cell', { name: 'Sealed' })).toHaveCSS('color', 'rgb(0, 0, 0)');
        if (width === 320) {
          await page.keyboard.press('Tab');
          const region = page.getByRole('region', { name: 'Details — scroll horizontally', exact: true });
          await expect(region).toBeFocused();
          await expect(region).toHaveCSS('outline-style', 'solid');
          await expect(region).toHaveCSS('outline-width', '2px');
        }
      } else {
        const grid = page.locator('.ot-relationship-grid');
        await expect(grid).toHaveCSS('border-top-style', 'solid');
        await expect(grid).toHaveCSS('border-top-width', '1px');
        if (width === 1280) {
          await expect(page.locator('.ot-relationship-table thead th').first()).toHaveCSS('border-bottom-style', 'solid');
          if (state === 'selected') await expect(page.locator('.ot-relationship-table tbody tr').nth(1).locator('th')).toHaveCSS('border-top-style', 'solid');
        } else if (state === 'selected') {
          await expect(page.locator('.ot-relationship-record').nth(1)).toHaveCSS('border-top-style', 'solid');
        }
        if (state === 'selected') await expect(page.locator('.ot-relationship-reference')).toHaveCSS('border-top-style', 'solid');
        if (state === 'empty') await expect(page.locator('.ot-relationship-empty')).toHaveCSS('border-top-style', 'solid');
        await expect(page.locator('.ot-relationship-grid :is(th, .ot-relationship-line)').first()).toHaveCSS('color', 'rgb(0, 0, 0)');
      }
      await capture(page, info, name);
    }
  }
});
