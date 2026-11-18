import { test, expect, type Locator, type Page, type TestInfo } from '@playwright/test';
import { build } from 'vite';
import { resolve } from 'node:path';
import { writeFile } from 'node:fs/promises';

const origin = process.env.OPENTOUR_FIXTURE_ORIGIN ?? 'http://127.0.0.1:4317';
let html = '';
test.beforeAll(async () => {
  const entry = resolve(import.meta.dirname, '__single_choice_select_fixture.tsx');
  const bundles = await build({ configFile: false, root: import.meta.dirname, logLevel: 'error',
    plugins: [{ name: 'finite-single-choice-fixture', resolveId(id) { if (id === entry) return entry; },
      load(id) { if (id === entry) return `
        import { createRoot } from 'react-dom/client';
        import { flushSync } from 'react-dom';
        import { SingleChoiceSelectFixture } from './single-choice-select.fixtures';
        import tokens from '../../../styles/product-tokens.css?inline';
        import select from './single-choice-select.css?inline';
        import fixtures from './single-choice-select.fixtures.css?inline';
        const style = document.createElement('style'); style.textContent = tokens + select + fixtures; document.head.append(style);
        const root = createRoot(document.querySelector('#root'));
        flushSync(() => root.render(<SingleChoiceSelectFixture />));
      `; }, }],
    build: { write: false, assetsInlineLimit: 1024 * 1024,
      rolldownOptions: { input: entry, output: { format: 'iife' } } },
  });
  const bundle = Array.isArray(bundles) ? bundles[0] : bundles;
  if (!('output' in bundle)) throw new Error('Expected compiled single-choice fixture.');
  const js = bundle.output.find(item => item.type === 'chunk');
  const css = bundle.output.flatMap(item => item.type === 'asset' && item.fileName.endsWith('.css') ? [item.source] : []).join('\n');
  if (!js) throw new Error('Missing single-choice fixture bundle.');
  html = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<style>html{background:#fafbfd}body{margin:0}' + css + '</style></head><body><div id="root"></div><script>' +
    js.code.replaceAll('</script', '<\\/script') + '</script></body></html>';
});

const action = 'rgb(36, 91, 238)';
const subtle = 'rgb(237, 243, 255)';
const base = 'rgb(255, 255, 255)';

/** Only the generated document is served; every other request is refused. */
async function open(page: Page, width = 1280, height = 900, scale = 100) {
  await page.setViewportSize({ width, height });
  await page.route('**/*', route => route.request().url() === origin + '/' ? route.fulfill({ contentType: 'text/html', body: html }) : route.abort());
  await page.goto(origin);
  await page.evaluate(percent => { document.documentElement.style.fontSize = `${percent}%`; }, scale);
  await page.evaluate(async () => { await document.fonts.load('400 12px Geist'); await document.fonts.load('500 14px Geist'); await document.fonts.load('600 24px Geist'); await document.fonts.ready; });
  expect(await page.evaluate(() => [...document.fonts].some(face => face.family === 'Geist' && face.status === 'loaded'))).toBe(true);
  await expect(page.getByRole('heading', { name: 'Single-choice select' })).toBeVisible();
}
const combobox = (page: Page, name: string) => page.getByRole('combobox', { name, exact: true });
const listbox = (page: Page, name: string) => page.getByRole('listbox', { name, exact: true });
/** The option whose title starts its accessible name; the state line follows the title. */
const option = (list: Locator, title: string) => list.getByRole('option', { name: new RegExp(`^${title}`) });
/** The bordered frame is the trigger's parent; the component root, which also holds the list, is above it. */
const frameOf = (trigger: Locator) => trigger.locator('xpath=..');
const rootOf = (trigger: Locator) => trigger.locator('xpath=../..');
const valueOf = (trigger: Locator) => trigger.locator('.ot-single-choice-value');
const stateOf = (row: Locator) => row.locator('.ot-single-choice-option-state');
const titleOf = (row: Locator) => row.locator('.ot-single-choice-option-title');
const requested = (page: Page) => page.getByRole('status', { name: 'Requested choices' });
/** The focus ring is the outline of the trigger's stretched pseudo-element, which covers the whole frame. */
const ring = (trigger: Locator) => trigger.evaluate(node => {
  const style = getComputedStyle(node, '::after');
  return { style: style.outlineStyle, width: style.outlineWidth, color: style.outlineColor, offset: style.outlineOffset };
});
/** Longhands only: the computed outline shorthand is not serialized the same way by every engine build. */
async function expectOutline(row: Locator, width: string, color: string) {
  await expect(row).toHaveCSS('outline-style', 'solid');
  await expect(row).toHaveCSS('outline-width', width);
  await expect(row).toHaveCSS('outline-color', color);
}
/** The logical highlight: one row, named as the trigger's active descendant, however it is drawn. */
async function expectHighlighted(trigger: Locator, list: Locator, row: Locator) {
  await expect(row).toHaveAttribute('data-highlighted', 'true');
  await expect(list.locator('[data-highlighted]')).toHaveCount(1);
  await expect(trigger).toHaveAttribute('aria-activedescendant', (await row.getAttribute('id'))!);
}
/** The highlight made visible by the keyboard or a pointer over the row: the action ring inside one row's box. */
async function expectRinged(list: Locator, row: Locator) {
  await expect(row).toHaveAttribute('data-highlighted', 'true');
  await expect(row).toHaveAttribute('data-highlight-ring', 'true');
  await expect(list.locator('[data-highlight-ring]')).toHaveCount(1);
  await expectOutline(row, '2px', action);
  await expect(row).toHaveCSS('outline-offset', '-2px');
  await expect(row).toHaveCSS('background-color', subtle);
}
/** A list opened by a press: there is still a highlighted row, and no row is ringed. */
async function expectNeutral(list: Locator) {
  await expect(list.locator('[data-highlighted]')).toHaveCount(1);
  await expect(list.locator('[data-highlight-ring]')).toHaveCount(0);
  for (const row of await list.getByRole('option').all()) await expect(row).toHaveCSS('outline-style', 'none');
}
type Box = { x: number; y: number; width: number; height: number };
/** An element's border box in document coordinates, which a full-page clip is measured in. */
const documentBox = (target: Locator): Promise<Box> => target.evaluate(node => {
  const box = node.getBoundingClientRect();
  return { x: box.x + window.scrollX, y: box.y + window.scrollY, width: box.width, height: box.height };
});
/** The focus ring reaches 4px past the frame (2px offset, then 2px ring); a capture keeps twice that around its target. */
const ringMargin = 8;
/**
 * One real capture of an element with a margin on every side, so a focus ring around it is whole in the
 * image instead of being cropped to the element's own box. The clip may not leave the document.
 */
async function captureAround(page: Page, target: Locator) {
  const box = await documentBox(target);
  const x = Math.floor(box.x) - ringMargin;
  const y = Math.floor(box.y) - ringMargin;
  const clip: Box = { x, y, width: Math.ceil(box.x + box.width) + ringMargin - x, height: Math.ceil(box.y + box.height) + ringMargin - y };
  const [pageWidth, pageHeight] = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.scrollHeight]);
  expect(Math.min(clip.x, clip.y)).toBeGreaterThanOrEqual(0);
  expect(clip.x + clip.width).toBeLessThanOrEqual(pageWidth);
  expect(clip.y + clip.height).toBeLessThanOrEqual(pageHeight);
  return { clip, shot: await page.screenshot({ fullPage: true, clip, animations: 'disabled' }) };
}
/**
 * Reads the capture's own pixels. Walking outwards from the middle of each side of the frame there are
 * two pixels of clear offset, two of ring, then clear again: the ring is 2px wide, 2px off the frame,
 * present on all four sides and not cut off by the edge of the image.
 */
async function expectWholeRing(page: Page, capture: { clip: Box; shot: Buffer }, frame: Locator, color: string) {
  const box = await documentBox(frame);
  const left = Math.round(box.x) - capture.clip.x;
  const top = Math.round(box.y) - capture.clip.y;
  const right = Math.round(box.x + box.width) - capture.clip.x;
  const bottom = Math.round(box.y + box.height) - capture.clip.y;
  const middle = [Math.round((left + right) / 2), Math.round((top + bottom) / 2)];
  const outwards = [0, 1, 2, 3, 4, 5];
  const points = [
    ...outwards.map(step => [middle[0], top - 1 - step]), ...outwards.map(step => [middle[0], bottom + step]),
    ...outwards.map(step => [left - 1 - step, middle[1]]), ...outwards.map(step => [right + step, middle[1]]),
  ];
  const read = await page.evaluate(async ({ data, points }) => {
    const image = new Image(); image.src = `data:image/png;base64,${data}`; await image.decode();
    const canvas = document.createElement('canvas'); canvas.width = image.width; canvas.height = image.height;
    const context = canvas.getContext('2d')!; context.drawImage(image, 0, 0);
    return { size: [image.width, image.height], colors: points.map(([x, y]) => {
      const [red, green, blue] = context.getImageData(x, y, 1, 1).data;
      return `rgb(${red}, ${green}, ${blue})`;
    }) };
  }, { data: capture.shot.toString('base64'), points });
  expect(read.size).toEqual([capture.clip.width, capture.clip.height]);
  const painted = read.colors.map(value => value === color);
  for (const side of [0, 1, 2, 3]) {
    expect(painted.slice(side * outwards.length, (side + 1) * outwards.length)).toEqual([false, false, true, true, false, false]);
  }
}
async function expectNoOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  expect(await page.locator('main').evaluate(node => [...node.querySelectorAll('*')].filter(element =>
    element.scrollWidth > element.clientWidth + 1 && getComputedStyle(element).display !== 'inline').map(element => element.className))).toEqual([]);
}
/** The retained file and the attachment are the same bytes of one real browser capture. */
async function retain(info: TestInfo, name: string, shot: Buffer) {
  await writeFile(info.outputPath(name), shot);
  await info.attach(name, { body: shot, contentType: 'image/png' });
}

for (const [width, height] of [[320, 800], [768, 1024], [1280, 900], [1440, 1024]] as const) {
  for (const scale of [100, 200] as const) {
    test(`single-choice select states at ${width}px and ${scale}% text`, async ({ page }, info) => {
      await open(page, width, height, scale);
      const ratio = scale / 100;
      const name = (state: string) => `single-choice-${width}-${scale}-${state}.png`;
      const select = combobox(page, 'Select');
      const frame = frameOf(select);
      const label = frame.locator('.ot-single-choice-label');
      const support = frame.locator('.ot-single-choice-support');

      // Closed (native 12:39): name, value, description and the designed type and frame.
      await expect(select).toHaveAttribute('aria-expanded', 'false');
      await expect(select).toHaveAttribute('aria-haspopup', 'listbox');
      await expect(select).toHaveAccessibleDescription('Supporting information');
      await expect(valueOf(select)).toHaveText('Select a value');
      expect(await select.evaluate(node => node.textContent)).toBe('Select a value  ▾');
      await expect(select.locator('[aria-hidden="true"]')).toHaveCount(1);
      await expect(listbox(page, 'Select')).toBeHidden();
      for (const text of [label, select]) {
        await expect(text).toHaveCSS('font-family', 'Geist, sans-serif');
        await expect(text).toHaveCSS('font-size', `${14 * ratio}px`);
        await expect(text).toHaveCSS('line-height', `${20 * ratio}px`);
        await expect(text).toHaveCSS('font-weight', '500');
        await expect(text).toHaveCSS('color', 'rgb(24, 34, 55)');
      }
      await expect(support).toHaveCSS('font-family', 'Geist, sans-serif');
      await expect(support).toHaveCSS('font-size', `${12 * ratio}px`);
      await expect(support).toHaveCSS('line-height', `${18 * ratio}px`);
      await expect(support).toHaveCSS('font-weight', '400');
      await expect(support).toHaveCSS('color', 'rgb(81, 96, 120)');
      await expect(frame).toHaveCSS('border', '1px solid rgb(98, 111, 132)');
      await expect(frame).toHaveCSS('border-radius', '8px');
      await expect(frame).toHaveCSS('padding', '16px');
      await expect(frame).toHaveCSS('row-gap', '8px');
      await expect(frame).toHaveCSS('background-color', base);
      const rest = (await frame.boundingBox())!;
      const restLabel = (await label.boundingBox())!;
      const restValue = (await select.boundingBox())!;
      expect(rest.width).toBe(width === 320 ? 288 : 360);
      if (scale === 100) {
        const helper = (await support.boundingBox())!;
        expect(rest.height).toBe(108);
        expect([restLabel.x - rest.x, restLabel.y - rest.y, restLabel.width, restLabel.height]).toEqual([17, 17, rest.width - 34, 20]);
        expect([restValue.x - rest.x, restValue.y - rest.y, restValue.width, restValue.height]).toEqual([17, 45, rest.width - 34, 20]);
        expect([helper.x - rest.x, helper.y - rest.y, helper.width, helper.height]).toEqual([17, 73, rest.width - 34, 18]);
      }
      // The whole frame is the target, well past 44px each way: a point in its padding still lands on the trigger.
      expect(Math.min(rest.width, rest.height)).toBeGreaterThanOrEqual(44);
      expect(await page.evaluate(([x, y]) => document.elementFromPoint(x, y)?.getAttribute('role'),
        [rest.x + rest.width / 2, rest.y + rest.height - 4])).toBe('combobox');
      // At rest nothing is ringed, on the frame's target or on the trigger itself.
      expect((await ring(select)).style).toBe('none');
      await expect(select).toHaveCSS('outline-style', 'none');
      await retain(info, name('closed'), await frame.screenshot({ animations: 'disabled' }));

      // Hover (native 152:18): a real pointer over the frame; nothing moves.
      await frame.hover();
      await expect(frame).toHaveCSS('background-color', subtle);
      await expect(frame).toHaveCSS('border', '1px solid rgb(98, 111, 132)');
      expect(await frame.boundingBox()).toEqual(rest);
      await retain(info, name('hover'), await frame.screenshot({ animations: 'disabled' }));
      await page.mouse.move(0, 0);
      await expect(frame).toHaveCSS('background-color', base);

      // Focus (native 152:14, handoff G07, G08 and D06): real keyboard focus draws a 2px action ring, 2px
      // clear of the whole frame. The frame keeps its own border and nothing moves or resizes.
      await page.keyboard.press('Tab');
      await expect(select).toBeFocused();
      expect(await ring(select)).toEqual({ style: 'solid', width: '2px', color: action, offset: '2px' });
      await expect(frame).toHaveCSS('border', '1px solid rgb(98, 111, 132)');
      await expect(frame).toHaveCSS('background-color', base);
      expect(await frame.boundingBox()).toEqual(rest);
      expect(await label.boundingBox()).toEqual(restLabel);
      expect(await select.boundingBox()).toEqual(restValue);
      // The retained image is wider than the frame, and its pixels hold the ring on every side.
      const focused = await captureAround(page, frame);
      await expectWholeRing(page, focused, frame, action);
      await retain(info, name('focus'), focused.shot);

      // Open (native 12:43): the keyboard opens the list in flow under the unchanged frame.
      await page.keyboard.press('Enter');
      const candidates = listbox(page, 'Select');
      await expect(select).toHaveAttribute('aria-expanded', 'true');
      await expect(candidates).toBeVisible();
      expect(await candidates.getAttribute('id')).toBe(await select.getAttribute('aria-controls'));
      await expect(candidates.getByRole('option')).toHaveCount(3);
      await expect(candidates.getByRole('option', { selected: true })).toHaveCount(0);
      await expectHighlighted(select, candidates, option(candidates, 'Candidate r2'));
      // Opened by the keyboard, the highlighted row is visibly ringed although nothing is selected.
      await expectRinged(candidates, option(candidates, 'Candidate r2'));
      await expect(stateOf(option(candidates, 'Candidate r2'))).toHaveText('Available · eligible');
      await expect(select).toBeFocused();
      const opened = (await frame.boundingBox())!;
      expect([opened.width, opened.height]).toEqual([rest.width, rest.height]);
      await expectNoOverflow(page);
      // The control is still keyboard-focused, so this image keeps its whole ring as well.
      const openedCapture = await captureAround(page, rootOf(select));
      await expectWholeRing(page, openedCapture, frame, action);
      await retain(info, name('open'), openedCapture.shot);
      await page.keyboard.press('Escape');
      await expect(candidates).toBeHidden();
      await expect(select).toBeFocused();
      await expect(valueOf(select)).toHaveText('Select a value');
      expect(await select.evaluate(node => node.textContent)).toBe('Select a value  ▾');

      // Error (native 12:47, product copy 166:9963): failure surface and text, with the message linked.
      // The contextual value is exactly its words: the caret belongs to the generic placeholder only.
      const required = combobox(page, 'Candidate artifact · Required');
      const requiredFrame = frameOf(required);
      await expect(required).toHaveAttribute('aria-invalid', 'true');
      await expect(required).toHaveAccessibleDescription('Choose an eligible artifact before continuing.');
      await expect(valueOf(required)).toHaveText('Choose an eligible artifact');
      expect(await required.evaluate(node => node.textContent)).toBe('Choose an eligible artifact');
      await expect(required.locator('[aria-hidden="true"]')).toHaveCount(0);
      await expect(requiredFrame).toHaveCSS('background-color', 'rgb(255, 232, 227)');
      await expect(requiredFrame).toHaveCSS('border', '1px solid rgb(98, 111, 132)');
      for (const text of [requiredFrame.locator('.ot-single-choice-label'), required, requiredFrame.locator('.ot-single-choice-support')]) {
        await expect(text).toHaveCSS('color', 'rgb(170, 69, 58)');
      }
      await expect(select).not.toHaveAttribute('aria-invalid');
      await retain(info, name('error'), await requiredFrame.screenshot({ animations: 'disabled' }));

      // Disabled (native 12:51): native disabled semantics, the designed opacity and a linked visible reason.
      const unavailable = combobox(page, 'Candidate artifact');
      const unavailableFrame = frameOf(unavailable);
      await expect(unavailable).toBeDisabled();
      await expect(unavailable).toHaveAttribute('aria-expanded', 'false');
      await expect(unavailable).toHaveAccessibleDescription('Complete artifact qualification, then refresh this list.');
      await expect(unavailableFrame.locator('.ot-single-choice-support')).toBeVisible();
      await expect(valueOf(unavailable)).toHaveText('No eligible artifacts');
      expect(await unavailable.evaluate(node => node.textContent)).toBe('No eligible artifacts');
      await expect(unavailable.locator('[aria-hidden="true"]')).toHaveCount(0);
      await expect(unavailableFrame).toHaveCSS('opacity', '0.55');
      await unavailableFrame.hover();
      await expect(unavailableFrame).toHaveCSS('background-color', base);
      await unavailable.click({ force: true });
      await expect(unavailable).toHaveAttribute('aria-expanded', 'false');
      await page.mouse.move(0, 0);
      await retain(info, name('disabled'), await unavailableFrame.screenshot({ animations: 'disabled' }));

      // Menu (native 31:36, value 31:38): a press opens the reference rows neutral. The selected row is
      // the active descendant and carries the subtle fill alone; no row is ringed.
      const source = combobox(page, 'Source');
      const sources = listbox(page, 'Source');
      const [refund, policy, notes] = ['Refund ledger', 'Policy registry', 'Operator notes'].map(title => option(sources, title));
      await source.click();
      await page.mouse.move(0, 0);
      await expect(sources).toBeVisible();
      await expect(source).toBeFocused();
      await expect(source).toHaveAccessibleName('Source');
      await expect(valueOf(source)).toHaveText('Refund ledger');
      // The chosen value is exactly its label: no caret is typed after it anywhere in the reference.
      expect(await source.evaluate(node => node.textContent)).toBe('Refund ledger');
      await expect(source.locator('[aria-hidden="true"]')).toHaveCount(0);
      await expect(page.getByTestId('reference').getByText('▾')).toHaveCount(0);
      await expect(source).toHaveAccessibleDescription('Arrow keys move focus; Enter selects; Escape closes.');
      await expect(sources.getByRole('option')).toHaveCount(3);
      await expect(titleOf(refund)).toHaveText('Refund ledger');
      await expect(stateOf(refund)).toHaveText('Selected · completion authority');
      await expect(titleOf(policy)).toHaveText('Policy registry');
      await expect(stateOf(policy)).toHaveText('Available · policy authority');
      await expect(titleOf(notes)).toHaveText('Operator notes');
      await expect(stateOf(notes)).toHaveText('Disabled · not independent truth');
      await expect(refund).toHaveAttribute('aria-selected', 'true');
      await expect(policy).toHaveAttribute('aria-selected', 'false');
      await expect(notes).toHaveAttribute('aria-disabled', 'true');
      await expectHighlighted(source, sources, refund);
      await expectNeutral(sources);
      await expect(refund).toHaveCSS('background-color', subtle);
      await expect(policy).toHaveCSS('background-color', base);
      await expect(notes).toHaveCSS('background-color', base);
      for (const row of [refund, policy, notes]) {
        await expect(row).toHaveCSS('padding', '12px');
        await expect(row).toHaveCSS('row-gap', '4px');
        await expect(titleOf(row)).toHaveCSS('font-size', `${14 * ratio}px`);
        await expect(titleOf(row)).toHaveCSS('line-height', `${20 * ratio}px`);
        await expect(titleOf(row)).toHaveCSS('font-weight', '500');
        await expect(stateOf(row)).toHaveCSS('font-size', `${12 * ratio}px`);
        await expect(stateOf(row)).toHaveCSS('line-height', `${18 * ratio}px`);
        await expect(stateOf(row)).toHaveCSS('color', 'rgb(81, 96, 120)');
      }
      const sourceFrame = (await frameOf(source).boundingBox())!;
      const rows = await Promise.all([refund, policy, notes].map(async row => (await row.boundingBox())!));
      expect(sourceFrame.width).toBe(width === 320 ? 256 : 544);
      expect(rows.map(row => [row.x, row.width])).toEqual(rows.map(() => [sourceFrame.x, sourceFrame.width]));
      expect(rows[0].y - (sourceFrame.y + sourceFrame.height)).toBe(12);
      expect(rows[1].y - (rows[0].y + rows[0].height)).toBe(12);
      expect(rows[2].y - (rows[1].y + rows[1].height)).toBe(12);
      if (scale === 100) expect(rows.map(row => row.height)).toEqual([66, 66, 66]);
      await expectNoOverflow(page);
      await retain(info, name('menu'), await page.getByTestId('reference').screenshot({ animations: 'disabled' }));
      // The keyboard then makes the highlight visible: the row it moves to is ringed, the selected row
      // keeps its fill alone, and an idle row has neither.
      await page.keyboard.press('ArrowDown');
      await expectHighlighted(source, sources, policy);
      await expectRinged(sources, policy);
      await expect(refund).toHaveAttribute('aria-selected', 'true');
      await expect(refund).toHaveCSS('background-color', subtle);
      await expect(refund).toHaveCSS('outline-style', 'none');
      await expect(notes).toHaveCSS('background-color', base);
      await expect(notes).toHaveCSS('outline-style', 'none');
      await page.keyboard.press('Escape');
      await expect(sources).toBeHidden();
      await expect(source).toBeFocused();
      await expect(valueOf(source)).toHaveText('Refund ledger');
      await expect(requested(page)).toHaveText('');
      await expectNoOverflow(page);
    });
  }
}

test('keyboard opens the list, skips disabled choices and never changes the value while moving', async ({ page }, info) => {
  await open(page);
  const review = combobox(page, 'Review source');
  const list = listbox(page, 'Review source');
  const [refund, notes, policy, audit] = ['Refund ledger', 'Operator notes', 'Policy registry', 'Audit export'].map(title => option(list, title));
  await review.focus();
  await page.keyboard.press('ArrowDown');
  await expect(list).toBeVisible();
  await expect(review).toBeFocused();
  await expect(list.getByRole('option')).toHaveCount(4);
  await expect(notes).toHaveAttribute('aria-disabled', 'true');
  await expectHighlighted(review, list, refund);
  // The keyboard always shows where it is, on the selected row too: the fill and the ring together.
  await expect(refund).toHaveAttribute('aria-selected', 'true');
  await expectRinged(list, refund);
  await page.keyboard.press('ArrowDown'); await expectHighlighted(review, list, policy);
  await expectRinged(list, policy);
  await expect(refund).toHaveCSS('background-color', subtle);
  await expect(refund).toHaveCSS('outline-style', 'none');
  await page.keyboard.press('ArrowDown'); await expectHighlighted(review, list, audit);
  await page.keyboard.press('ArrowDown'); await expectHighlighted(review, list, audit);
  await page.keyboard.press('ArrowUp'); await expectHighlighted(review, list, policy);
  await page.keyboard.press('ArrowUp'); await expectHighlighted(review, list, refund);
  await page.keyboard.press('ArrowUp'); await expectHighlighted(review, list, refund);
  await page.keyboard.press('End'); await expectHighlighted(review, list, audit);
  await page.keyboard.press('Home'); await expectHighlighted(review, list, refund);
  await page.keyboard.press('ArrowDown'); await expectHighlighted(review, list, policy);
  await expect(notes).not.toHaveAttribute('data-highlighted');
  // Moving the highlight is not a choice: nothing was requested and the selection is where it was.
  await expect(requested(page)).toHaveText('');
  await expect(valueOf(review)).toHaveText('Refund ledger');
  await expect(refund).toHaveAttribute('aria-selected', 'true');
  await expect(policy).toHaveAttribute('aria-selected', 'false');
  await expect(review).toBeFocused();
  await expectRinged(list, policy);
  // The margin keeps any focus ring on the control whole instead of cropping it to the component's box.
  await retain(info, 'single-choice-keyboard-highlight.png', (await captureAround(page, rootOf(review))).shot);
});

test('Enter and Space request the highlighted choice, close the list and keep focus on the trigger', async ({ page }, info) => {
  await open(page);
  const review = combobox(page, 'Review source');
  const list = listbox(page, 'Review source');
  await review.focus();
  await page.keyboard.press('Enter');
  await expect(list).toBeVisible();
  await expect(requested(page)).toHaveText('');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(list).toBeHidden();
  await expect(review).toBeFocused();
  await expect(review).toHaveAttribute('aria-expanded', 'false');
  await expect(review).not.toHaveAttribute('aria-activedescendant');
  await expect(requested(page)).toHaveText('review:policy-registry');
  await expect(valueOf(review)).toHaveText('Policy registry');
  // Space opens with a single toggle, on the new selection.
  await page.keyboard.press('Space');
  await expect(list).toBeVisible();
  await expectHighlighted(review, list, option(list, 'Policy registry'));
  await expectRinged(list, option(list, 'Policy registry'));
  await expect(option(list, 'Policy registry')).toHaveAttribute('aria-selected', 'true');
  await expect(stateOf(option(list, 'Policy registry'))).toHaveText('Selected · policy authority');
  await expect(stateOf(option(list, 'Refund ledger'))).toHaveText('Available · completion authority');
  await expect(list.getByRole('option', { selected: true })).toHaveCount(1);
  await page.keyboard.press('ArrowDown');
  await expectHighlighted(review, list, option(list, 'Audit export'));
  // The highlight has moved on; the selected row keeps its fill and gives up the ring.
  await expectRinged(list, option(list, 'Audit export'));
  await expect(option(list, 'Policy registry')).toHaveCSS('background-color', subtle);
  await expect(option(list, 'Policy registry')).toHaveCSS('outline-style', 'none');
  await retain(info, 'single-choice-selected.png', (await captureAround(page, rootOf(review))).shot);
  await page.keyboard.press('Space');
  await expect(list).toBeHidden();
  await expect(review).toBeFocused();
  await expect(requested(page)).toHaveText('review:policy-registry, review:audit-export');
  await expect(valueOf(review)).toHaveText('Audit export');
  // Confirming the current value is not a new request.
  await page.keyboard.press('Enter');
  await expectHighlighted(review, list, option(list, 'Audit export'));
  await page.keyboard.press('Enter');
  await expect(list).toBeHidden();
  await expect(requested(page)).toHaveText('review:policy-registry, review:audit-export');
});

test('Escape and Tab dismiss without a request, and Tab moves focus on', async ({ page }) => {
  await open(page);
  const source = combobox(page, 'Source');
  const list = listbox(page, 'Source');
  await source.focus();
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await expectHighlighted(source, list, option(list, 'Policy registry'));
  await page.keyboard.press('Escape');
  await expect(list).toBeHidden();
  await expect(source).toBeFocused();
  await expect(source).toHaveAttribute('aria-expanded', 'false');
  await expect(source).not.toHaveAttribute('aria-activedescendant');
  await expect(valueOf(source)).toHaveText('Refund ledger');
  await expect(requested(page)).toHaveText('');
  // Escape on a closed control is left alone.
  await page.keyboard.press('Escape');
  await expect(source).toBeFocused();
  await expect(list).toBeHidden();
  // Tab closes the list without choosing the highlighted row and focus moves forward.
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await expectHighlighted(source, list, option(list, 'Policy registry'));
  await page.keyboard.press('Tab');
  await expect(list).toBeHidden();
  await expect(combobox(page, 'Held source')).toBeFocused();
  await expect(valueOf(source)).toHaveText('Refund ledger');
  await expect(requested(page)).toHaveText('');
  // Shift+Tab does the same in the other direction; the disabled control is not a stop.
  await page.keyboard.press('Shift+Tab');
  await expect(source).toBeFocused();
  await expect(list).toBeHidden();
  await page.keyboard.press('Enter');
  await expect(list).toBeVisible();
  await page.keyboard.press('Shift+Tab');
  await expect(list).toBeHidden();
  await expect(combobox(page, 'Candidate artifact · Required')).toBeFocused();
  await expect(valueOf(source)).toHaveText('Refund ledger');
  await expect(requested(page)).toHaveText('');
});

test('pointer opens the list, hover highlights without choosing and a click requests the choice', async ({ page }, info) => {
  await open(page);
  const source = combobox(page, 'Source');
  const list = listbox(page, 'Source');
  const refund = option(list, 'Refund ledger');
  const policy = option(list, 'Policy registry');
  await source.click();
  await expect(list).toBeVisible();
  await expect(source).toBeFocused();
  await expectHighlighted(source, list, refund);
  // Opened by a press, the list is neutral (native 31:36): the selected row is still the active
  // descendant and keeps its subtle fill, and no row is ringed.
  await expectNeutral(list);
  await expect(refund).toHaveCSS('background-color', subtle);
  await expect(policy).toHaveCSS('background-color', base);
  // A pointer over a row rings it, the selected row like any other, so hover never looks like selection alone.
  await refund.hover();
  await expectHighlighted(source, list, refund);
  await expectRinged(list, refund);
  await policy.hover();
  await expectHighlighted(source, list, policy);
  await expectRinged(list, policy);
  await expect(refund).not.toHaveAttribute('data-highlighted');
  await expect(refund).toHaveAttribute('aria-selected', 'true');
  await expect(policy).toHaveAttribute('aria-selected', 'false');
  await expectOutline(policy, '2px', action);
  await expect(refund).toHaveCSS('outline-style', 'none');
  await expect(refund).toHaveCSS('background-color', subtle);
  await expect(requested(page)).toHaveText('');
  await expect(valueOf(source)).toHaveText('Refund ledger');
  await expect(source).toBeFocused();
  await retain(info, 'single-choice-pointer-highlight.png', await page.getByTestId('reference').screenshot({ animations: 'disabled' }));
  await policy.click();
  await expect(list).toBeHidden();
  await expect(source).toBeFocused();
  await expect(requested(page)).toHaveText('source:policy-registry');
  await expect(valueOf(source)).toHaveText('Policy registry');
  // Pressing the trigger again opens on the new selection, and pressing it once more closes.
  await source.click();
  await expect(list).toBeVisible();
  await expect(stateOf(policy)).toHaveText('Selected · policy authority');
  await expect(stateOf(refund)).toHaveText('Available · completion authority');
  await expect(list.getByRole('option', { selected: true })).toHaveCount(1);
  await source.click();
  await expect(list).toBeHidden();
  await expect(source).toBeFocused();
  await expect(requested(page)).toHaveText('source:policy-registry');
});

test('a disabled choice shows its reason and blocks requests from pointer and keyboard', async ({ page }) => {
  await open(page);
  const source = combobox(page, 'Source');
  const list = listbox(page, 'Source');
  const refund = option(list, 'Refund ledger');
  const policy = option(list, 'Policy registry');
  const notes = option(list, 'Operator notes');
  await source.click();
  await expect(notes).toHaveAttribute('aria-disabled', 'true');
  await expect(notes).toHaveAttribute('aria-selected', 'false');
  await expect(stateOf(notes)).toBeVisible();
  await expect(stateOf(notes)).toHaveText('Disabled · not independent truth');
  await expect(notes).toHaveCSS('cursor', 'not-allowed');
  // The pointer cannot highlight it and a press on it is blocked: the list stays open, nothing is requested.
  await notes.hover();
  await expectHighlighted(source, list, refund);
  await expectNeutral(list);
  await notes.click({ force: true });
  await expect(list).toBeVisible();
  await expect(source).toBeFocused();
  await expect(requested(page)).toHaveText('');
  await expect(valueOf(source)).toHaveText('Refund ledger');
  await expect(notes).toHaveAttribute('aria-selected', 'false');
  // The keyboard cannot reach it either.
  await page.keyboard.press('End');
  await expectHighlighted(source, list, policy);
  await expectRinged(list, policy);
  await page.keyboard.press('ArrowDown');
  await expectHighlighted(source, list, policy);
  await expect(notes).not.toHaveAttribute('data-highlighted');
  await expect(notes).toHaveCSS('outline-style', 'none');
  await page.keyboard.press('Escape');
  await expect(list).toBeHidden();
  await expect(source).toBeFocused();
  await expect(requested(page)).toHaveText('');
});

test('a press outside dismisses without a request and returns focus to the trigger', async ({ page }) => {
  await open(page);
  const source = combobox(page, 'Source');
  const list = listbox(page, 'Source');
  await source.focus();
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await expectHighlighted(source, list, option(list, 'Policy registry'));
  await page.getByRole('heading', { name: 'Single-choice select' }).click();
  await expect(list).toBeHidden();
  await expect(source).toBeFocused();
  await expect(valueOf(source)).toHaveText('Refund ledger');
  await expect(requested(page)).toHaveText('');
  // A press on another control dismisses too, and focus stays where that press put it.
  await page.keyboard.press('Enter');
  await expect(list).toBeVisible();
  const next = page.getByRole('button', { name: 'Next control' });
  await next.click();
  await expect(list).toBeHidden();
  await expect(next).toBeFocused();
  await expect(valueOf(source)).toHaveText('Refund ledger');
  await expect(requested(page)).toHaveText('');
});

test('the caller owns the value: an unaccepted request leaves the shown value unchanged', async ({ page }) => {
  await open(page);
  const held = combobox(page, 'Held source');
  const list = listbox(page, 'Held source');
  await held.click();
  await option(list, 'Policy registry').click();
  // Park the pointer so the reopened rows are not under it.
  await page.mouse.move(0, 0);
  await expect(list).toBeHidden();
  await expect(held).toBeFocused();
  await expect(requested(page)).toHaveText('held:policy-registry');
  await expect(valueOf(held)).toHaveText('Refund ledger');
  await page.keyboard.press('Enter');
  await expect(option(list, 'Refund ledger')).toHaveAttribute('aria-selected', 'true');
  await expect(option(list, 'Policy registry')).toHaveAttribute('aria-selected', 'false');
  await expectHighlighted(held, list, option(list, 'Refund ledger'));
  // The same request can be made again; the control still does not decide the outcome.
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(requested(page)).toHaveText('held:policy-registry, held:policy-registry');
  await expect(valueOf(held)).toHaveText('Refund ledger');
  // A control with no value behaves the same way.
  const required = combobox(page, 'Candidate artifact · Required');
  await required.click();
  await option(listbox(page, 'Candidate artifact · Required'), 'Candidate r3').click();
  await expect(requested(page)).toHaveText('held:policy-registry, held:policy-registry, required:candidate-r3');
  await expect(valueOf(required)).toHaveText('Choose an eligible artifact');
  await expect(required).toHaveAttribute('aria-invalid', 'true');
});

test('instances are independent, and a press on a control below an open list still reaches it', async ({ page }) => {
  await open(page);
  const source = combobox(page, 'Source');
  const held = combobox(page, 'Held source');
  const review = combobox(page, 'Review source');
  const ids = await page.evaluate(() => [...document.querySelectorAll('main [id]')].map(node => node.id));
  expect(new Set(ids).size).toBe(ids.length);
  expect(new Set(await Promise.all([source, held, review].map(trigger => trigger.getAttribute('aria-controls')))).size).toBe(3);
  // Opening one instance by pressing it closes the open one above it; each keeps its own value.
  await source.click();
  await expect(listbox(page, 'Source')).toBeVisible();
  await review.click();
  await expect(listbox(page, 'Source')).toBeHidden();
  await expect(listbox(page, 'Review source')).toBeVisible();
  await expect(page.getByRole('listbox')).toHaveCount(1);
  await expect(review).toBeFocused();
  await expect(source).toHaveAttribute('aria-expanded', 'false');
  await option(listbox(page, 'Review source'), 'Audit export').click();
  await expect(valueOf(review)).toHaveText('Audit export');
  await expect(valueOf(source)).toHaveText('Refund ledger');
  await expect(valueOf(held)).toHaveText('Refund ledger');
  await expect(requested(page)).toHaveText('review:audit-export');
  // The button sits below the open in-flow list; its press must not be lost when the list closes.
  await source.click();
  await expect(listbox(page, 'Source')).toBeVisible();
  await page.getByRole('button', { name: 'Replace options' }).click();
  await expect(listbox(page, 'Source')).toBeHidden();
  await expect(page.getByRole('button', { name: 'Replace options' })).toBeFocused();
  await review.click();
  await expect(listbox(page, 'Review source').getByRole('option')).toHaveCount(2);
});

test('a replaced option list is followed without a request or a stale choice', async ({ page }) => {
  await open(page);
  const review = combobox(page, 'Review source');
  const list = listbox(page, 'Review source');
  await expect(valueOf(review)).toHaveText('Refund ledger');
  // Replace the list while it is open, with a highlighted row that survives the change. The gallery's
  // own event stands in for a caller re-rendering; a press on its button would be an outside dismissal.
  await review.focus();
  await page.keyboard.press('End');
  await expect(list.getByRole('option')).toHaveCount(4);
  await expectHighlighted(review, list, option(list, 'Audit export'));
  await page.evaluate(() => { document.dispatchEvent(new Event('single-choice-fixture:replace-options')); });
  await expect(list.getByRole('option')).toHaveCount(2);
  await expect(review).toBeFocused();
  await expectHighlighted(review, list, option(list, 'Audit export'));
  // The current value is no longer offered: it is shown as no choice, and nothing is requested for it.
  // With no choice the generic placeholder is back, typed with its caret as the design draws it.
  await expect(valueOf(review)).toHaveText('Select a value');
  expect(await review.evaluate(node => node.textContent)).toBe('Select a value  ▾');
  await expect(list.getByRole('option', { selected: true })).toHaveCount(0);
  await expect(requested(page)).toHaveText('');
  await expect(stateOf(option(list, 'Policy registry'))).toHaveText('Available · policy authority');
  await page.keyboard.press('Home');
  await expectHighlighted(review, list, option(list, 'Policy registry'));
  await page.keyboard.press('Enter');
  await expect(list).toBeHidden();
  await expect(requested(page)).toHaveText('review:policy-registry');
  await expect(valueOf(review)).toHaveText('Policy registry');
});

test('forced colors keep the frame, focus, selection and highlight at 320px and 200% text', async ({ page }, info) => {
  await page.emulateMedia({ forcedColors: 'active' });
  await open(page, 320, 800, 200);
  const system = await page.evaluate(() => {
    const swatch = document.createElement('span');
    swatch.style.forcedColorAdjust = 'none';
    document.body.append(swatch);
    const colors = ['Highlight', 'SelectedItem', 'CanvasText'].map(color => { swatch.style.color = color; return getComputedStyle(swatch).color; });
    swatch.remove();
    return colors;
  });
  const [highlight, selectedItem, canvasText] = system;
  const source = combobox(page, 'Source');
  const list = listbox(page, 'Source');
  // Real keyboard focus: the disabled control is not a stop, so Source is the third.
  for (let stop = 0; stop < 3; stop += 1) await page.keyboard.press('Tab');
  await expect(source).toBeFocused();
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await expect(source).toBeFocused();
  // The ring keeps its width and offset and takes the system highlight colour; the reference section's
  // padding is wider than the ring's reach, so the retained image below holds all of it.
  expect(await ring(source)).toEqual({ style: 'solid', width: '2px', color: highlight, offset: '2px' });
  await expect(frameOf(source)).toHaveCSS('border-top-style', 'solid');
  await expect(frameOf(source)).toHaveCSS('border-top-width', '1px');
  await expect(option(list, 'Refund ledger')).toHaveAttribute('aria-selected', 'true');
  await expect(option(list, 'Refund ledger')).toHaveCSS('background-color', selectedItem);
  await expectOutline(option(list, 'Refund ledger'), '1px', canvasText);
  await expectHighlighted(source, list, option(list, 'Policy registry'));
  await expectOutline(option(list, 'Policy registry'), '3px', highlight);
  await expectOutline(option(list, 'Operator notes'), '1px', canvasText);
  await expect(stateOf(option(list, 'Operator notes'))).toHaveText('Disabled · not independent truth');
  await expect(frameOf(combobox(page, 'Candidate artifact'))).toHaveCSS('opacity', '1');
  await expectNoOverflow(page);
  await retain(info, 'single-choice-forced-colors.png', await page.getByTestId('reference').screenshot({ animations: 'disabled' }));
});
