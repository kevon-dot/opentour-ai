import { expect, test } from '@playwright/test';
import type { Locator, Page } from '@playwright/test';
import { join } from 'node:path';
import { build } from 'vite';

const origin = 'http://form-errors.test';
const views = ['mapped', 'unmapped', 'form', 'duplicate'] as const;
type View = typeof views[number];

const mappedBody = 'Your entries are still here. Correct the fields below.';
const unmappedTitle = 'We could not save these changes';
const unmappedBody = 'Review the form and try again. Contact support if the problem continues.';
const attemptsRow = 'Maximum attempts: enter a whole number';
const spendRow = 'Maximum spend: enter a nonnegative amount';
const generalRow = 'These limits could not be saved together.';

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
    return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html lang="en"><title>Form error summary fixture</title>' +
      `<main data-view="${path}"></main></html>` });
  });
});

async function open(page: Page, view: View, scale = 100) {
  await page.goto(`${origin}/${view}`);
  await page.addStyleTag({ content: `${styles}html{font-size:${scale}%}body{margin:0}` });
  await page.addScriptTag({ type: 'module', url: `${origin}/gallery.js` });
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  await page.evaluate(async () => {
    for (const font of ['400 12px Geist', '400 14px Geist', '500 14px Geist', '600 18px Geist']) await document.fonts.load(font);
    await document.fonts.ready;
  });
  // A capture or measurement in a fallback face would not be evidence for the design.
  expect(await page.evaluate(() => [...document.fonts].some(face => face.family === 'Geist' && face.status === 'loaded'))).toBe(true);
}

const mappedTitle = (count: number) => `Check ${count} ${count === 1 ? 'field' : 'fields'} before continuing`;
const summaryNamed = (scope: Page | Locator, name: string) => scope.getByRole('region', { name, exact: true });
const composedForm = (page: Page) => page.getByRole('form', { name: 'Generated run limits', exact: true });
const attemptsIn = (form: Locator) => form.getByRole('textbox', { name: 'Maximum attempts', exact: true });
const spendIn = (form: Locator) => form.getByRole('textbox', { name: 'Maximum spend', exact: true });

/** Fills both limits with entries that fail and submits from the keyboard, as a person would. */
async function submitInvalid(form: Locator) {
  await attemptsIn(form).fill('2.5');
  await spendIn(form).fill('-4');
  await spendIn(form).press('Enter');
}

/** Nothing in the summary is cut off: no part scrolls, hides overflow or lies outside the summary box. */
const fits = (summary: Locator) => summary.evaluate(node => {
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

    // Standalone mapped summary, in a 360px box and outside the form that owns its inputs.
    await open(page, 'mapped', scale);
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
    // The summary is outside the form here and reaches the form's own input through the form id.
    const owned = page.getByRole('form', { name: 'Temporary native inputs', exact: true });
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

    // Composed form after a failed keyboard submission: the summary stretches to the form and holds focus.
    await open(page, 'form', scale);
    const form = composedForm(page);
    await submitInvalid(form);
    const summary = summaryNamed(form, mappedTitle(2));
    await expect(summary).toBeFocused();
    await expect(summary).toHaveCSS('outline-style', 'solid');
    await expect(summary).toHaveCSS('outline-width', '2px');
    await expect(summary).toHaveCSS('outline-color', 'rgb(36, 91, 238)');
    await expect(summary).toHaveCSS('outline-offset', '2px');
    await expect(summary.getByRole('link')).toHaveText([attemptsRow, spendRow]);
    await expect(attemptsIn(form)).toHaveValue('2.5');
    await expect(spendIn(form)).toHaveValue('-4');
    expect((await summary.boundingBox())!.width).toBe(Math.min(688, width - 2 * pad));
    expect(await fits(summary)).toBe(true);
    expect(await noSidewaysScroll(page)).toBe(true);
    await page.locator('.form-errors-gallery').screenshot({ path: info.outputPath(`form-errors-${width}-${scale}-form.png`) });
  });
}

test('zero, one, two, mixed and unmapped errors follow the current errors', async ({ page }) => {
  await open(page, 'form');
  const form = composedForm(page);
  const attempts = attemptsIn(form);
  const spend = spendIn(form);
  const outcome = form.getByRole('combobox', { name: 'Generated save outcome', exact: true });
  const activity = form.getByRole('status', { name: 'Generated form activity', exact: true });
  const anySummary = form.locator('.ot-form-error-summary');

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
  const form = composedForm(page);
  const attempts = attemptsIn(form);
  const spend = spendIn(form);
  await submitInvalid(form);
  const summary = summaryNamed(form, mappedTitle(2));
  const links = summary.getByRole('link');
  await expect(summary).toBeFocused();
  await expect(attempts).toHaveValue('2.5');
  await expect(spend).toHaveValue('-4');
  await expect(attempts).toHaveAttribute('aria-invalid', 'true');

  // The summary is not a Tab stop of its own: the first Tab from it lands on the first link.
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

  // Back to the second link, which blurs the first input with its entry unchanged.
  await page.keyboard.press('Shift+Tab');
  await expect(links.nth(1)).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(spend).toBeFocused();
  await expect(spend).toHaveValue('-4');
  await expect(attempts).toHaveValue('2.5');

  // A pointer activation reaches the same input.
  await links.nth(0).click();
  await expect(attempts).toBeFocused();
  await expect(links).toHaveText([attemptsRow, spendRow]);
});

test('only a new invalid submission moves focus to the summary', async ({ page }) => {
  await open(page, 'form');
  const form = composedForm(page);
  const attempts = attemptsIn(form);
  const spend = spendIn(form);
  const again = form.getByRole('button', { name: 'Render again', exact: true });
  await submitInvalid(form);
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
  const form = composedForm(page);
  // The adverse condition is real: two elements carry each id, and the earlier one comes first in the document.
  await expect(page.locator('[id="maximum-attempts"]')).toHaveCount(2);
  await expect(page.locator('[id="maximum-spend"]')).toHaveCount(2);
  expect(await page.evaluate(() => document.getElementById('maximum-attempts')?.closest('form')?.getAttribute('aria-label')))
    .toBe('Earlier generated form');

  await submitInvalid(form);
  const summary = summaryNamed(form, mappedTitle(2));
  const links = summary.getByRole('link');
  await expect(summary).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(links.nth(0)).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(attemptsIn(form)).toBeFocused();
  await expect(attemptsIn(earlier)).not.toBeFocused();
  await links.nth(1).click();
  await expect(spendIn(form)).toBeFocused();
  await expect(spendIn(earlier)).not.toBeFocused();
  await expect(attemptsIn(earlier)).toHaveValue('earlier 1');
  await expect(spendIn(earlier)).toHaveValue('earlier 2');
  await expect(attemptsIn(form)).toHaveValue('2.5');
  await expect(spendIn(form)).toHaveValue('-4');
});

test('forced colors keep the notice boundary, readable text, links and focus indication', async ({ page }, info) => {
  await page.setViewportSize({ width: 320, height: 900 });
  await page.emulateMedia({ forcedColors: 'active' });
  await open(page, 'form');
  const form = composedForm(page);
  await submitInvalid(form);
  const summary = summaryNamed(form, mappedTitle(2));
  const notice = summary.getByRole('status');
  const link = summary.getByRole('link').first();
  const [highlight] = await systemColors(page, ['Highlight']);
  await expect(summary).toBeFocused();
  await expect(summary).toHaveCSS('outline-style', 'solid');
  await expect(summary).toHaveCSS('outline-width', '2px');
  await expect(summary).toHaveCSS('outline-color', highlight);
  await expect(notice).toHaveCSS('border-top-style', 'solid');
  await expect(notice).toHaveCSS('border-top-width', '1px');
  // The boundary, the notice text and the link text each differ from the surface they sit on.
  const painted = await summary.evaluate(node => {
    const color = (selector: string, property: 'borderTopColor' | 'backgroundColor' | 'color') =>
      getComputedStyle(node.querySelector(selector)!)[property];
    return {
      surface: color('.ot-notice', 'backgroundColor'), boundary: color('.ot-notice', 'borderTopColor'),
      title: color('.ot-notice-title', 'color'), body: color('.ot-notice-body', 'color'), link: color('a', 'color'),
    };
  });
  for (const part of [painted.boundary, painted.title, painted.body, painted.link]) expect(part).not.toBe(painted.surface);
  await expect(link).toHaveCSS('text-decoration-line', 'underline');
  expect(await fits(summary)).toBe(true);
  expect(await noSidewaysScroll(page)).toBe(true);
  await page.locator('.form-errors-gallery').screenshot({ path: info.outputPath('form-errors-forced-colors.png') });
  await page.keyboard.press('Tab');
  await expect(link).toBeFocused();
  await expect(link).toHaveCSS('outline-width', '2px');
  await expect(link).toHaveCSS('outline-color', highlight);
});
