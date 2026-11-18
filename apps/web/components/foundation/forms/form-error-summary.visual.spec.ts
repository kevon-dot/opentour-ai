import { expect, test } from '@playwright/test';
import type { Locator, Page } from '@playwright/test';
import { join } from 'node:path';
import { build } from 'vite';

const origin = 'http://form-errors.test';
const views = ['mapped', 'unmapped', 'form', 'stress', 'duplicate'] as const;
type View = typeof views[number];

const mappedBody = 'Your entries are still here. Correct the fields below.';
const unmappedTitle = 'We could not save these changes';
const unmappedBody = 'Review the form and try again. Contact support if the problem continues.';
const attemptsRow = 'Maximum attempts: enter a whole number';
const spendRow = 'Maximum spend: enter a nonnegative amount';
const generalRow = 'These limits could not be saved together.';
/** The amount exactly as a person typed it, with a true minus sign (U+2212). It is never parsed or rewritten. */
const rawSpend = 'USD −10.00';
/** Every piece of text on the workflow settings screen after the failed submission, in markup order (Figma 159:121). */
const workflowCopy = [
  'OpenTour  /  Customer workspace', 'Acme Labs / Production',
  'Workflow settings', 'Receipt-status workflow · Current revision 8',
  'Check 2 fields before continuing', mappedBody,
  'Workflow name · Required', 'Name used in your workspace.',
  'Maximum attempts · Required', 'Enter a whole number. Your entry has not been changed.',
  'Maximum spend · Required', 'Enter a nonnegative amount.',
  attemptsRow, spendRow,
  'Save changes', 'Discard draft',
];

let script: string;
let styles: string;
test.beforeAll(async () => {
  const root = join(process.cwd(), 'apps/web');
  const result = await build({
    root, configFile: false, envFile: false, publicDir: false, logLevel: 'error',
    oxc: { jsx: { runtime: 'automatic' } },
    build: { write: false, lib: { entry: join(root, 'tests/fixtures/presentation/form-error-summary.tsx'), formats: ['es'] } },
    define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  });
  const bundle = Array.isArray(result) ? result[0] : result;
  if (!('output' in bundle)) throw new Error('Missing form error gallery');
  const js = bundle.output.find(item => item.type === 'chunk');
  const css = bundle.output.find(item => item.type === 'asset' && item.fileName.endsWith('.css'));
  if (!js || js.type !== 'chunk' || !css || css.type !== 'asset') throw new Error('Missing form error gallery assets');
  script = js.code;
  styles = String(css.source);
});

test.beforeEach(async ({ page }) => {
  await page.route(origin + '/**', route => {
    const path = new URL(route.request().url()).pathname.slice(1);
    if (path === 'gallery.js') return route.fulfill({ contentType: 'text/javascript', body: script });
    if (!views.some(view => view === path)) return route.abort();
    // Each view renders its own landmarks, so the mount is a plain element.
    return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html lang="en"><title>Form error summary fixture</title>' +
      `<div data-view="${path}"></div></html>` });
  });
});

async function open(page: Page, view: View, scale = 100) {
  await page.goto(`${origin}/${view}`);
  await page.addStyleTag({ content: `${styles}html{font-size:${scale}%}body{margin:0}` });
  await page.addScriptTag({ type: 'module', url: `${origin}/gallery.js` });
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  await page.evaluate(async () => {
    for (const font of ['400 12px Geist', '400 14px Geist', '500 14px Geist', '600 18px Geist', '600 24px Geist']) {
      await document.fonts.load(font);
    }
    await document.fonts.ready;
  });
  // A capture or measurement in a fallback face would not be evidence for the design.
  expect(await page.evaluate(() => [...document.fonts].some(face => face.family === 'Geist' && face.status === 'loaded'))).toBe(true);
}

const mappedTitle = (count: number) => `Check ${count} ${count === 1 ? 'field' : 'fields'} before continuing`;
const summaryNamed = (scope: Page | Locator, name: string) => scope.getByRole('region', { name, exact: true });
/** The rows of a detached summary: a list after the fields, named by the summary's heading. */
const detachedRows = (form: Locator, name: string) => form.getByRole('list', { name, exact: true });
const settingsForm = (page: Page) => page.getByRole('form', { name: 'Workflow settings', exact: true });
const limitsForm = (page: Page) => page.getByRole('form', { name: 'Generated run limits', exact: true });
/** A shared field, found by the label the production TextField gives its native input. */
const requiredField = (form: Locator, label: string) => form.getByRole('textbox', { name: `${label} · Required`, exact: true });
const attemptsIn = (form: Locator) => requiredField(form, 'Maximum attempts');
const spendIn = (form: Locator) => requiredField(form, 'Maximum spend');

/** Fills both limits with entries that fail and submits from the keyboard, as a person would. */
async function submitInvalid(attempts: Locator, spend: Locator, amount = '-4') {
  await attempts.fill('2.5');
  await spend.fill(amount);
  await spend.press('Enter');
}

/** The native input is the production control inside the production field frame, not a fixture stand-in. */
async function expectProductionField(input: Locator, id: string) {
  await expect(input).toHaveClass('ot-text-field-control');
  await expect(input).toHaveAttribute('id', id);
  expect(await input.evaluate(node => node.localName === 'input' && node.parentElement?.className === 'ot-text-field')).toBe(true);
}

/** Every non-blank text node under the scope, in markup order and exactly as written. */
const textIn = (scope: Locator) => scope.evaluate(node => {
  const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
  const texts: string[] = [];
  while (walker.nextNode()) {
    const text = walker.currentNode.nodeValue ?? '';
    if (text.trim().length > 0) texts.push(text);
  }
  return texts;
});

/** Ids carried by more than one element. A regular view has none; only the adverse decoy repeats one. */
const repeatedIds = (page: Page) => page.evaluate(() => {
  const ids = [...document.querySelectorAll('[id]')].map(node => node.id);
  return ids.filter((id, index) => ids.indexOf(id) !== index);
});

/** Nothing in the part is cut off: no element scrolls, hides overflow or lies outside the part's box. */
const fits = (part: Locator) => part.evaluate(node => {
  const outer = node.getBoundingClientRect();
  return [node, ...node.querySelectorAll('*')].every(element => {
    const style = getComputedStyle(element);
    return style.overflowX === 'visible' && style.overflowY === 'visible' && style.textOverflow === 'clip' &&
      (style.display === 'inline' || element.scrollWidth <= element.clientWidth + 1) &&
      [...element.getClientRects()].every(rect => rect.left >= outer.left - 0.5 && rect.right <= outer.right + 0.5 &&
        rect.top >= outer.top - 0.5 && rect.bottom <= outer.bottom + 0.5);
  });
});
const noSidewaysScroll = (page: Page) => page.evaluate(() =>
  document.documentElement.scrollWidth <= document.documentElement.clientWidth);
const pageBox = async (part: Locator) => {
  const box = (await part.boundingBox())!;
  return [box.x, box.y, box.width, box.height];
};

async function expectType(text: Locator, size: number, line: number, weight: string, scale: number) {
  await expect(text).toHaveCSS('font-family', 'Geist, sans-serif');
  await expect(text).toHaveCSS('font-size', `${size * scale / 100}px`);
  await expect(text).toHaveCSS('line-height', `${line * scale / 100}px`);
  await expect(text).toHaveCSS('font-weight', weight);
}

const systemColors = (page: Page, names: string[]) => page.evaluate(names => {
  const swatch = document.createElement('span');
  swatch.style.forcedColorAdjust = 'none';
  document.body.append(swatch);
  const colors = names.map(name => { swatch.style.color = name; return getComputedStyle(swatch).color; });
  swatch.remove();
  return colors;
}, names);

for (const width of [320, 768, 1280, 1440]) for (const scale of [100, 200]) {
  test(`form error summary at ${width}px and ${scale}% text`, async ({ page }, info) => {
    const pad = width === 320 ? 16 : width === 768 ? 24 : 32;
    const specimen = Math.min(360, width - 2 * pad);
    const exact = scale === 100 && specimen === 360;
    await page.setViewportSize({ width, height: 900 });

    // Standalone mapped summary, in a 360px box and outside the form that owns its fields.
    await open(page, 'mapped', scale);
    expect(await repeatedIds(page)).toEqual([]);
    const mapped = summaryNamed(page, mappedTitle(2));
    const notice = mapped.getByRole('status');
    const links = mapped.getByRole('link');
    const rows = mapped.getByRole('listitem');
    await expect(mapped.locator('.ot-notice-title')).toHaveText(mappedTitle(2));
    await expect(mapped.locator('.ot-notice-body')).toHaveText(mappedBody);
    await expect(links).toHaveText([attemptsRow, spendRow]);
    await expect(links.nth(0)).toHaveAttribute('href', '#maximum-attempts');
    await expect(links.nth(1)).toHaveAttribute('href', '#maximum-spend');
    await expect(mapped).toHaveAttribute('tabindex', '-1');
    // No submission has been reported, so rendering alone leaves focus where it was.
    await expect(mapped).not.toBeFocused();
    await expect(notice).toHaveCSS('border-top-width', '1px');
    await expect(notice).toHaveCSS('border-top-style', 'solid');
    await expect(notice).toHaveCSS('border-top-color', 'rgb(227, 232, 240)');
    await expect(notice).toHaveCSS('border-top-left-radius', '8px');
    await expect(notice).toHaveCSS('padding-top', '16px');
    await expect(notice).toHaveCSS('padding-left', '16px');
    await expect(notice).toHaveCSS('row-gap', '8px');
    await expect(mapped).toHaveCSS('row-gap', '12px');
    await expect(mapped.getByRole('list')).toHaveCSS('row-gap', '12px');
    await expectType(mapped.locator('.ot-notice-title'), 14, 20, '500', scale);
    await expectType(mapped.locator('.ot-notice-body'), 12, 18, '400', scale);
    for (const link of [links.nth(0), links.nth(1)]) {
      await expectType(link, 14, 22, '400', scale);
      await expect(link).toHaveCSS('color', 'rgb(36, 91, 238)');
      await expect(link).toHaveCSS('text-decoration-line', 'underline');
    }
    const bounds = (await mapped.boundingBox())!;
    expect(bounds.width).toBe(specimen);
    if (exact) {
      const within = async (part: Locator) => {
        const box = (await part.boundingBox())!;
        return [box.x - bounds.x, box.y - bounds.y, box.width, box.height];
      };
      // 80px notice (1 + 16 + 20 + 8 + 18 + 16 + 1), then two 22px rows, each after a 12px gap.
      expect(bounds.height).toBe(148);
      expect(await within(notice)).toEqual([0, 0, 360, 80]);
      expect(await within(rows.nth(0))).toEqual([0, 92, 360, 22]);
      expect(await within(rows.nth(1))).toEqual([0, 126, 360, 22]);
    }
    expect(await fits(mapped)).toBe(true);
    expect(await noSidewaysScroll(page)).toBe(true);
    await mapped.screenshot({ path: info.outputPath(`form-errors-${width}-${scale}-mapped.png`) });
    // The summary is outside the form here and reaches the form's own shared field through the form id.
    const owned = limitsForm(page);
    await expectProductionField(attemptsIn(owned), 'maximum-attempts');
    await expectProductionField(spendIn(owned), 'maximum-spend');
    await links.nth(1).focus();
    await page.keyboard.press('Enter');
    await expect(spendIn(owned)).toBeFocused();
    await expect(spendIn(owned)).toHaveValue('-4');

    // Standalone unmapped summary: the notice alone.
    await open(page, 'unmapped', scale);
    const unmapped = summaryNamed(page, unmappedTitle);
    await expect(unmapped.locator('.ot-notice-title')).toHaveText(unmappedTitle);
    await expect(unmapped.locator('.ot-notice-body')).toHaveText(unmappedBody);
    await expect(unmapped.getByRole('list')).toHaveCount(0);
    await expect(unmapped.getByRole('link')).toHaveCount(0);
    const unmappedBounds = (await unmapped.boundingBox())!;
    expect(unmappedBounds.width).toBe(specimen);
    if (exact) {
      // The body wraps to two 18px lines at this width: 1 + 16 + 20 + 8 + 36 + 16 + 1.
      expect((await unmapped.locator('.ot-notice-body').boundingBox())!.height).toBe(36);
      expect(unmappedBounds.height).toBe(98);
    }
    expect(await fits(unmapped)).toBe(true);
    expect(await noSidewaysScroll(page)).toBe(true);
    await unmapped.screenshot({ path: info.outputPath(`form-errors-${width}-${scale}-unmapped.png`) });

    // The workflow settings screen (Figma 159:121, native frame 1440 x 852) after an actual failed keyboard
    // submission: one notice above the shared fields, its rows after them, and the notice region holding focus.
    await page.setViewportSize({ width, height: 852 });
    await open(page, 'form', scale);
    expect(await repeatedIds(page)).toEqual([]);
    const header = page.locator('.workflow-settings-header');
    const heading = page.getByRole('heading', { level: 1, name: 'Workflow settings', exact: true });
    const revision = page.locator('.workflow-settings-revision');
    const form = settingsForm(page);
    const name = requiredField(form, 'Workflow name');
    const attempts = attemptsIn(form);
    const spend = spendIn(form);
    const save = form.getByRole('button', { name: 'Save changes', exact: true });
    const discard = form.getByRole('button', { name: 'Discard draft', exact: true });
    await submitInvalid(attempts, spend, rawSpend);
    const summary = summaryNamed(form, mappedTitle(2));
    const rowList = detachedRows(form, mappedTitle(2));
    const formLinks = rowList.getByRole('link');
    await expect(summary).toBeFocused();
    await expect(summary).toHaveCSS('outline-style', 'solid');
    await expect(summary).toHaveCSS('outline-width', '2px');
    await expect(summary).toHaveCSS('outline-color', 'rgb(36, 91, 238)');
    await expect(summary).toHaveCSS('outline-offset', '2px');
    // One status for the whole form. The region holds the notice alone and the rows follow the fields.
    await expect(form.getByRole('status')).toHaveCount(1);
    await expect(summary.locator('.ot-notice-title')).toHaveText(mappedTitle(2));
    await expect(summary.locator('.ot-notice-body')).toHaveText(mappedBody);
    await expect(summary.getByRole('link')).toHaveCount(0);
    await expect(formLinks).toHaveText([attemptsRow, spendRow]);
    await expect(formLinks.nth(0)).toHaveAttribute('href', '#maximum-attempts');
    await expect(formLinks.nth(1)).toHaveAttribute('href', '#maximum-spend');
    for (const link of [formLinks.nth(0), formLinks.nth(1)]) {
      await expectType(link, 14, 22, '400', scale);
      await expect(link).toHaveCSS('color', 'rgb(36, 91, 238)');
      await expect(link).toHaveCSS('text-decoration-line', 'underline');
    }
    await expect(rowList).toHaveCSS('row-gap', '12px');
    // The three controls are the production fields, and they are the form's only native controls.
    await expectProductionField(name, 'workflow-name');
    await expectProductionField(attempts, 'maximum-attempts');
    await expectProductionField(spend, 'maximum-spend');
    await expect(form.locator('input, select, textarea')).toHaveCount(3);
    await expect(name).toHaveValue('Receipt status');
    await expect(attempts).toHaveValue('2.5');
    expect(await spend.inputValue()).toBe(rawSpend);
    // The screen carries the approved copy and nothing else: no harness title, note or control.
    expect(await textIn(page.locator('body'))).toEqual(workflowCopy);
    // Markup order is the visual order: notice, fields, rows, then the actions.
    const tops: number[] = [];
    for (const part of [summary, name, attempts, spend, formLinks.nth(0), formLinks.nth(1), save]) {
      tops.push((await part.boundingBox())!.y);
    }
    expect(tops).toEqual([...tops].sort((a, b) => a - b));
    expect(new Set(tops).size).toBe(tops.length);
    // Type and paint of the product shell and the form surface.
    await expectType(page.locator('.workflow-settings-product'), 14, 20, '500', scale);
    await expectType(page.locator('.workflow-settings-workspace'), 12, 18, '400', scale);
    await expectType(heading, 24, 32, '600', scale);
    await expectType(revision, 14, 22, '400', scale);
    await expect(header).toHaveCSS('background-color', 'rgb(255, 255, 255)');
    await expect(page.locator('.workflow-settings-screen')).toHaveCSS('background-color', 'rgb(250, 251, 253)');
    await expect(form).toHaveCSS('background-color', 'rgb(255, 255, 255)');
    await expect(form).toHaveCSS('border-top-width', '1px');
    await expect(form).toHaveCSS('border-top-color', 'rgb(227, 232, 240)');
    await expect(form).toHaveCSS('border-top-left-radius', '12px');
    await expect(form).toHaveCSS('row-gap', '16px');
    // The form is 720px at most with a 16px inset, so the notice and each field are 688px at most.
    const formWidth = Math.min(720, width - 2 * pad);
    const formBox = (await form.boundingBox())!;
    expect([formBox.x, formBox.width]).toEqual([pad, formWidth]);
    expect((await summary.boundingBox())!.width).toBe(formWidth - 32);
    expect((await rowList.boundingBox())!.width).toBe(formWidth - 32);
    for (const input of [name, attempts, spend]) {
      expect((await input.locator('xpath=..').boundingBox())!.width).toBe(formWidth - 32);
    }
    const saveBox = (await save.boundingBox())!;
    const discardBox = (await discard.boundingBox())!;
    if (width === 320) {
      // Too narrow for two actions side by side: they stack at the form's measure.
      expect([saveBox.width, discardBox.width]).toEqual([formWidth - 32, formWidth - 32]);
      expect(discardBox.x).toBe(saveBox.x);
      expect(discardBox.y).toBeGreaterThan(saveBox.y);
    } else if (scale === 100) {
      expect([saveBox.width, discardBox.width]).toEqual([280, 280]);
      expect(discardBox.y).toBe(saveBox.y);
      expect(discardBox.x).toBeGreaterThan(saveBox.x + saveBox.width);
    }
    if (scale === 100) {
      // The header spans the viewport at 80px and its text starts 16px in.
      expect(await pageBox(header)).toEqual([0, 0, width, 80]);
      const product = (await page.locator('.workflow-settings-product').boundingBox())!;
      expect([product.x, product.y]).toEqual([16, 16]);
    }
    if (scale === 100 && width >= 1280) {
      // Native positions: 80 header, 32 inset, 32 heading, 16, 22 line, 16, then the form on a 16px rhythm
      // with an 80px notice, three 106px fields (1 + 15 + 20 + 8 + 20 + 8 + 18 + 15 + 1) and two 22px rows 12 apart.
      expect(await pageBox(heading)).toEqual([32, 112, width - 64, 32]);
      expect(await pageBox(revision)).toEqual([32, 160, width - 64, 22]);
      expect(formBox.y).toBe(198);
      expect(await pageBox(summary)).toEqual([48, 214, 688, 80]);
      expect(await pageBox(name.locator('xpath=..'))).toEqual([48, 310, 688, 106]);
      expect(await pageBox(attempts.locator('xpath=..'))).toEqual([48, 432, 688, 106]);
      expect(await pageBox(spend.locator('xpath=..'))).toEqual([48, 554, 688, 106]);
      expect(await pageBox(rowList.getByRole('listitem').nth(0))).toEqual([48, 676, 688, 22]);
      expect(await pageBox(rowList.getByRole('listitem').nth(1))).toEqual([48, 710, 688, 22]);
      expect([saveBox.x, saveBox.y]).toEqual([48, 748]);
    }
    if (scale === 100 && width === 1440) {
      // The whole screen fits the native frame, so the capture below is exactly 1440 x 852.
      expect(await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.scrollHeight])).toEqual([1440, 852]);
    }
    expect(await fits(summary)).toBe(true);
    expect(await fits(rowList)).toBe(true);
    expect(await noSidewaysScroll(page)).toBe(true);
    // The design frame shows the form at rest, so focus leaves the summary before the capture. The capture
    // is the whole screen at the viewport's width, product shell included, not a clip of an inner part.
    await summary.evaluate(node => node.blur());
    await expect(summary).not.toBeFocused();
    await page.screenshot({ path: info.outputPath(`form-errors-${width}-${scale}-form.png`), fullPage: true });
  });
}

test('zero, one, two, mixed and unmapped errors follow the current errors', async ({ page }) => {
  await open(page, 'stress');
  expect(await repeatedIds(page)).toEqual([]);
  const form = limitsForm(page);
  const attempts = attemptsIn(form);
  const spend = spendIn(form);
  const outcome = form.getByRole('combobox', { name: 'Generated save outcome', exact: true });
  const activity = form.getByRole('status', { name: 'Generated form activity', exact: true });
  const anySummary = form.locator('.ot-form-error-summary');
  await expectProductionField(attempts, 'maximum-attempts');
  await expectProductionField(spend, 'maximum-spend');

  // Zero: nothing is rendered, and an invalid entry that is only blurred before any submission reports nothing.
  await expect(anySummary).toHaveCount(0);
  await attempts.fill('2.5');
  await attempts.press('Tab');
  await expect(spend).toBeFocused();
  await expect(anySummary).toHaveCount(0);

  // One.
  await spend.fill('12.50');
  await spend.press('Enter');
  const one = summaryNamed(form, mappedTitle(1));
  await expect(one).toBeFocused();
  await expect(one.locator('.ot-notice-body')).toHaveText(mappedBody);
  await expect(one.getByRole('listitem')).toHaveText([attemptsRow]);
  await expect(one.getByRole('link')).toHaveText([attemptsRow]);
  await expect(activity).toHaveText('Not saved');

  // Two.
  await spend.fill('-4');
  await spend.press('Enter');
  const two = summaryNamed(form, mappedTitle(2));
  await expect(two).toBeFocused();
  await expect(two.getByRole('link')).toHaveText([attemptsRow, spendRow]);

  // Mixed: the unmapped text stays, as plain text after the links, and the heading counts fields only.
  await outcome.selectOption('general');
  await spend.press('Enter');
  await expect(two).toBeFocused();
  await expect(two.locator('.ot-notice-body')).toHaveText(mappedBody);
  await expect(two.getByRole('listitem')).toHaveText([attemptsRow, spendRow, generalRow]);
  await expect(two.getByRole('link')).toHaveText([attemptsRow, spendRow]);
  await expect(two.getByRole('listitem').nth(2).locator('a')).toHaveCount(0);
  await attempts.fill('3');
  await spend.press('Enter');
  await expect(one).toBeFocused();
  await expect(one.getByRole('listitem')).toHaveText([spendRow, generalRow]);
  await expect(one.getByRole('link')).toHaveText([spendRow]);

  // Unmapped with safe text: the general heading and body, and no link.
  await spend.fill('12.50');
  await spend.press('Enter');
  const unmapped = summaryNamed(form, unmappedTitle);
  await expect(unmapped).toBeFocused();
  await expect(unmapped.locator('.ot-notice-body')).toHaveText(unmappedBody);
  await expect(unmapped.getByRole('listitem')).toHaveText([generalRow]);
  await expect(unmapped.getByRole('link')).toHaveCount(0);

  // Unmapped with no public detail: the notice alone.
  await outcome.selectOption('undetailed');
  await spend.press('Enter');
  await expect(unmapped).toBeFocused();
  await expect(unmapped.getByRole('list')).toHaveCount(0);

  // Zero again: an accepted submission removes the summary and moves focus nowhere.
  await outcome.selectOption('accept');
  await spend.press('Enter');
  await expect(anySummary).toHaveCount(0);
  await expect(activity).toHaveText('Saved');
  await expect(spend).toBeFocused();
  await expect(attempts).toHaveValue('3');
  await expect(spend).toHaveValue('12.50');
});

test('raw entries survive a failed submission and a mapped link moves keyboard focus to the actual input', async ({ page }) => {
  await open(page, 'form');
  const form = settingsForm(page);
  const name = requiredField(form, 'Workflow name');
  const attempts = attemptsIn(form);
  const spend = spendIn(form);
  const fields = [name, attempts, spend];
  // The very input elements that exist before any error, to show later that none was replaced.
  const originals = await Promise.all(fields.map(input => input.elementHandle()));
  const sameElements = async () => {
    const kept: boolean[] = [];
    for (const [index, input] of fields.entries()) kept.push(await input.evaluate((node, original) => node === original, originals[index]));
    return kept;
  };
  await expect(form.locator('.ot-form-error-summary, .ot-form-error-list')).toHaveCount(0);

  await submitInvalid(attempts, spend, rawSpend);
  const summary = summaryNamed(form, mappedTitle(2));
  const links = detachedRows(form, mappedTitle(2)).getByRole('link');
  await expect(summary).toBeFocused();
  await expect(name).toHaveValue('Receipt status');
  await expect(attempts).toHaveValue('2.5');
  expect(await spend.inputValue()).toBe(rawSpend);
  expect(await name.getAttribute('aria-invalid')).toBeNull();
  await expect(attempts).toHaveAttribute('aria-invalid', 'true');
  await expect(spend).toHaveAttribute('aria-invalid', 'true');
  // Each invalid field is described by its own explanation, which the production field renders.
  await expect(attempts).toHaveAttribute('aria-describedby', 'maximum-attempts-error');
  await expect(form.locator('[id="maximum-attempts-error"]')).toHaveText('Enter a whole number. Your entry has not been changed.');
  await expect(spend).toHaveAttribute('aria-describedby', 'maximum-spend-error');
  await expect(form.locator('[id="maximum-spend-error"]')).toHaveText('Enter a nonnegative amount.');
  await expect(form.getByRole('status')).toHaveCount(1);
  expect(await sameElements()).toEqual([true, true, true]);

  // Keyboard order is markup order: from the summary, which is not a Tab stop of its own, through the
  // three fields to the first row.
  for (const input of fields) {
    await page.keyboard.press('Tab');
    await expect(input).toBeFocused();
  }
  await page.keyboard.press('Tab');
  await expect(links.nth(0)).toBeFocused();
  await expect(links.nth(0)).toHaveCSS('outline-style', 'solid');
  await expect(links.nth(0)).toHaveCSS('outline-width', '2px');
  await page.keyboard.press('Enter');
  await expect(attempts).toBeFocused();
  expect(await attempts.evaluate(node => node.id)).toBe('maximum-attempts');
  await expect(attempts).toHaveValue('2.5');
  // Focus was moved directly; the address did not change.
  expect(page.url()).toBe(`${origin}/form`);

  // On through the spend field and the first row to the second row, which leads to the spend field.
  for (const stop of [spend, links.nth(0), links.nth(1)]) {
    await page.keyboard.press('Tab');
    await expect(stop).toBeFocused();
  }
  await page.keyboard.press('Enter');
  await expect(spend).toBeFocused();
  expect(await spend.inputValue()).toBe(rawSpend);
  await expect(attempts).toHaveValue('2.5');
  expect(page.url()).toBe(`${origin}/form`);

  // A pointer activation reaches the same input.
  await links.nth(0).click();
  await expect(attempts).toBeFocused();
  await expect(links).toHaveText([attemptsRow, spendRow]);

  // A change of errors keeps every input in place: a corrected entry is re-checked on blur, the count drops,
  // focus stays where Tab put it and the other raw entry is untouched.
  await attempts.fill('3');
  await page.keyboard.press('Tab');
  await expect(summaryNamed(form, mappedTitle(1))).toBeVisible();
  await expect(detachedRows(form, mappedTitle(1)).getByRole('link')).toHaveText([spendRow]);
  await expect(spend).toBeFocused();
  expect(await spend.inputValue()).toBe(rawSpend);
  await expect(attempts).toHaveValue('3');
  expect(await attempts.getAttribute('aria-invalid')).toBeNull();
  await expect(form.getByRole('status')).toHaveCount(1);
  expect(await sameElements()).toEqual([true, true, true]);
});

test('only a new invalid submission moves focus to the summary', async ({ page }) => {
  await open(page, 'stress');
  const form = limitsForm(page);
  const attempts = attemptsIn(form);
  const spend = spendIn(form);
  const again = form.getByRole('button', { name: 'Render again', exact: true });
  await submitInvalid(attempts, spend);
  const two = summaryNamed(form, mappedTitle(2));
  const one = summaryNamed(form, mappedTitle(1));
  await expect(two).toBeFocused();

  // Editing alone: the entry changes, the errors and focus do not.
  await attempts.fill('3');
  await expect(attempts).toBeFocused();
  await expect(two).toBeVisible();

  // Blur after the first submission re-checks the field. The count drops and focus stays where Tab put it.
  await page.keyboard.press('Tab');
  await expect(one).toBeVisible();
  await expect(spend).toBeFocused();
  await expect(one.getByRole('link')).toHaveText([spendRow]);
  await expect(spend).toHaveValue('-4');
  await expect(attempts).toHaveValue('3');

  // The count can also rise on blur without taking focus.
  await attempts.fill('three');
  await page.keyboard.press('Tab');
  await expect(two).toBeVisible();
  await expect(spend).toBeFocused();
  await expect(attempts).toHaveValue('three');

  // A re-render with the same errors and the same submission leaves focus alone.
  await again.focus();
  await page.keyboard.press('Enter');
  await expect(form.getByRole('status', { name: 'Render count', exact: true })).toHaveText('1');
  await expect(again).toBeFocused();
  await expect(two).toBeVisible();

  // Each further invalid submission moves focus to the summary again, with the same errors or not.
  await spend.press('Enter');
  await expect(two).toBeFocused();
  await spend.focus();
  await expect(spend).toBeFocused();
  await spend.press('Enter');
  await expect(two).toBeFocused();
  await expect(two.getByRole('link')).toHaveText([attemptsRow, spendRow]);
  await expect(spend).toHaveValue('-4');
});

test('links move focus inside the active form when an earlier form repeats the field ids', async ({ page }) => {
  await open(page, 'duplicate');
  const earlier = page.getByRole('form', { name: 'Earlier generated form', exact: true });
  const form = limitsForm(page);
  // The adverse condition is real: two elements carry each id, and the earlier one comes first in the document.
  expect(await repeatedIds(page)).toEqual(['maximum-attempts', 'maximum-spend']);
  await expect(page.locator('[id="maximum-attempts"]')).toHaveCount(2);
  await expect(page.locator('[id="maximum-spend"]')).toHaveCount(2);
  expect(await page.evaluate(() => document.getElementById('maximum-attempts')?.closest('form')?.getAttribute('aria-label')))
    .toBe('Earlier generated form');
  // A repeated id also sends each label's `for` to the earlier form, so the active form's shared fields are
  // found here by the production control class and their id inside that form, not by their name.
  const attempts = form.locator('input.ot-text-field-control[id="maximum-attempts"]');
  const spend = form.locator('input.ot-text-field-control[id="maximum-spend"]');
  const earlierAttempts = earlier.getByRole('textbox', { name: 'Maximum attempts', exact: true });
  const earlierSpend = earlier.getByRole('textbox', { name: 'Maximum spend', exact: true });
  await expect(attempts).toHaveCount(1);
  await expect(spend).toHaveCount(1);

  await submitInvalid(attempts, spend);
  const summary = summaryNamed(form, mappedTitle(2));
  const links = summary.getByRole('link');
  await expect(summary).toBeFocused();
  // The attached summary is not a Tab stop of its own: the first Tab from it lands on the first link.
  await page.keyboard.press('Tab');
  await expect(links.nth(0)).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(attempts).toBeFocused();
  await expect(earlierAttempts).not.toBeFocused();
  await links.nth(1).click();
  await expect(spend).toBeFocused();
  await expect(earlierSpend).not.toBeFocused();
  await expect(earlierAttempts).toHaveValue('earlier 1');
  await expect(earlierSpend).toHaveValue('earlier 2');
  await expect(attempts).toHaveValue('2.5');
  await expect(spend).toHaveValue('-4');

  // Adverse: the field is taken out of the active form while its error is still listed. The only element
  // left with that id belongs to the earlier form. The link must not lead there by any route: focus stays
  // on the link, inside the active form, and native fragment navigation does not run.
  await form.getByRole('button', { name: 'Remove maximum spend field', exact: true }).click();
  await expect(spend).toHaveCount(0);
  await expect(page.locator('[id="maximum-spend"]')).toHaveCount(1);
  await expect(earlier.locator('[id="maximum-spend"]')).toHaveCount(1);
  await expect(links).toHaveText([attemptsRow, spendRow]);
  const focusOwner = () => page.evaluate(() => document.activeElement?.closest('form')?.getAttribute('aria-label') ?? null);
  await links.nth(1).focus();
  await page.keyboard.press('Enter');
  await expect(links.nth(1)).toBeFocused();
  await expect(earlierSpend).not.toBeFocused();
  expect(await focusOwner()).toBe('Generated run limits');
  expect(page.url()).toBe(`${origin}/duplicate`);
  await links.nth(1).click();
  await expect(links.nth(1)).toBeFocused();
  await expect(earlierSpend).not.toBeFocused();
  expect(await focusOwner()).toBe('Generated run limits');
  expect(page.url()).toBe(`${origin}/duplicate`);
  await expect(earlierSpend).toHaveValue('earlier 2');
  // The row whose field is still in the active form keeps working.
  await links.nth(0).focus();
  await page.keyboard.press('Enter');
  await expect(attempts).toBeFocused();
  await expect(earlierAttempts).not.toBeFocused();
  await expect(attempts).toHaveValue('2.5');
});

test('forced colors keep the notice boundary, readable text, links and focus indication', async ({ page }, info) => {
  await page.setViewportSize({ width: 320, height: 852 });
  await page.emulateMedia({ forcedColors: 'active' });
  await open(page, 'form');
  const form = settingsForm(page);
  const fields = [requiredField(form, 'Workflow name'), attemptsIn(form), spendIn(form)];
  await submitInvalid(fields[1], fields[2], rawSpend);
  const summary = summaryNamed(form, mappedTitle(2));
  const notice = summary.getByRole('status');
  const rowList = detachedRows(form, mappedTitle(2));
  const link = rowList.getByRole('link').first();
  const [highlight] = await systemColors(page, ['Highlight']);
  await expect(summary).toBeFocused();
  await expect(summary).toHaveCSS('outline-style', 'solid');
  await expect(summary).toHaveCSS('outline-width', '2px');
  await expect(summary).toHaveCSS('outline-color', highlight);
  await expect(notice).toHaveCSS('border-top-style', 'solid');
  await expect(notice).toHaveCSS('border-top-width', '1px');
  // The boundary and the notice text differ from the notice surface, and the link text from the form surface.
  const painted = await form.evaluate(node => {
    const color = (selector: string, property: 'borderTopColor' | 'backgroundColor' | 'color') =>
      getComputedStyle(node.querySelector(selector)!)[property];
    return {
      surface: color('.ot-notice', 'backgroundColor'), boundary: color('.ot-notice', 'borderTopColor'),
      title: color('.ot-notice-title', 'color'), body: color('.ot-notice-body', 'color'),
      formSurface: getComputedStyle(node).backgroundColor, link: color('.ot-form-error-link', 'color'),
    };
  });
  for (const part of [painted.boundary, painted.title, painted.body]) expect(part).not.toBe(painted.surface);
  expect(painted.link).not.toBe(painted.formSurface);
  await expect(link).toHaveCSS('text-decoration-line', 'underline');
  expect(await fits(summary)).toBe(true);
  expect(await fits(rowList)).toBe(true);
  expect(await noSidewaysScroll(page)).toBe(true);
  // Captured with the summary still focused, so the image shows the focus indication in forced colours.
  await page.screenshot({ path: info.outputPath('form-errors-forced-colors.png'), fullPage: true });
  // Through the three fields to the first row.
  for (const input of fields) {
    await page.keyboard.press('Tab');
    await expect(input).toBeFocused();
  }
  await page.keyboard.press('Tab');
  await expect(link).toBeFocused();
  await expect(link).toHaveCSS('outline-width', '2px');
  await expect(link).toHaveCSS('outline-color', highlight);
});
