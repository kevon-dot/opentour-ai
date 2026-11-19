import { test, expect } from '@playwright/test';
import type { Page, TestInfo } from '@playwright/test';
import { createHash } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
import { overlayFixtures as overlays, presentationFixtures as fixtures } from './fixtures';
import { decodePng } from '../../../tests/fixtures/harness/visual-comparison';

const unexpected = new WeakMap<Page, string[]>();
async function installBoundary(page: Page, baseURL: string | undefined) {
  const denied: string[] = []; unexpected.set(page, denied);
  await page.route('**/*', async route => {
    const request = route.request(); const url = new URL(request.url());
    if (url.origin !== baseURL || request.method() !== 'GET' ||
      !(url.pathname === '/' || url.pathname.startsWith('/assets/'))) {
      denied.push(`${request.method()}:${url.origin}${url.pathname}`); await route.abort(); return;
    }
    await route.continue();
  });
}
test.beforeEach(async ({ page, baseURL }) => { await installBoundary(page, baseURL); });
test.afterEach(async ({ page }) => { expect(unexpected.get(page)).toEqual([]); });

async function open(page: Page, info: TestInfo, width = 1440, tenant = 'a') {
  const height = new Map([[320, 800], [768, 1024], [1280, 900], [1440, 1024]]).get(width);
  if (height === undefined) throw new Error('Unknown presentation viewport');
  await page.setViewportSize({ width, height });
  await page.goto(`/?mode=${info.project.name}&tenant=${tenant}`);
  await expect(page.getByRole('heading', { name: fixtures.title })).toBeVisible();
  await page.evaluate(async () => {
    await Promise.all([document.fonts.load('400 14px Geist'), document.fonts.load('500 14px Geist'),
      document.fonts.load('600 24px Geist'), document.fonts.load('400 12px "Geist Mono"')]);
    await document.fonts.ready;
  });
  expect(await page.evaluate(() => document.fonts.check('500 14px Geist'))).toBe(true);
  expect(await page.evaluate(() => [...document.fonts].filter(font => font.status === 'loaded').map(font => font.family.replaceAll('"', '')).sort())).toEqual(['Geist', 'Geist Mono']);
}

async function capture(page: Page, info: TestInfo, name: string, clip?: { x: number; y: number; width: number; height: number }) {
  const bytes = await page.screenshot({ fullPage: !clip, clip, animations: 'disabled', caret: 'hide' });
  await writeFile(info.outputPath(`${name}.png`), bytes);
  await writeFile(info.outputPath(`${name}.json`), JSON.stringify({ name, mode: info.project.name,
    raw_png_sha256: createHash('sha256').update(bytes).digest('hex'), viewport: page.viewportSize(),
    threshold: 0, max_diff_pixels: 0, masks: [] }));
  expect(bytes).toMatchSnapshot(`${name}.png`, { threshold: 0, maxDiffPixels: 0 });
}

for (const width of [320, 768, 1280, 1440]) {
  test(`initial presentation at ${width}px retains all labels and exact visual baseline`, async ({ page }, info) => {
    await open(page, info, width);
    for (const badge of fixtures.badges) await expect(page.getByText(badge.label, { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    for (const button of await page.getByRole('button').all()) {
      const bounds = await button.boundingBox(); expect(bounds).not.toBeNull();
      expect(bounds!.x).toBeGreaterThanOrEqual(0); expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
    }
    await capture(page, info, `gallery-${width}`);
    await page.getByRole('tab', { name: 'Notes', exact: true }).scrollIntoViewIfNeeded();
    await expect(page.getByRole('tab', { name: 'Notes', exact: true })).toBeInViewport();
  });
}

for (const variant of fixtures.variants) {
  test(`${variant} button keyboard focus preserves geometry and uses the approved separated outline`, async ({ page }, info) => {
    await open(page, info);
    const button = page.locator(`[data-variant="${variant}"] button`).first();
    const before = await button.boundingBox();
    await page.keyboard.press('Tab'); // Enter keyboard modality before programmatic focus.
    await button.focus(); await expect(button).toBeFocused();
    const after = await button.boundingBox(); expect(after).toEqual(before);
    expect(await button.evaluate(node => {
      const style = getComputedStyle(node); return [style.outlineWidth, style.outlineOffset, style.outlineColor, style.outlineStyle];
    })).toEqual(['2px', '2px', 'rgb(36, 91, 238)', 'solid']);
    await capture(page, info, `focus-${variant}`, { x: after!.x - 6, y: after!.y - 6, width: after!.width + 12, height: after!.height + 12 });
    await page.keyboard.press('Enter');
    await expect(page.getByRole('status', { name: fixtures.invocationLabel })).toHaveText('1');
    await button.hover();
    if (variant === 'primary') expect(await button.evaluate(node => getComputedStyle(node).backgroundColor)).toBe('rgb(25, 68, 186)');
  });
}

test('busy and native disabled controls suppress activation while the busy name and focus persist', async ({ page }, info) => {
  await open(page, info);
  const begin = page.getByRole('button', { name: fixtures.busyLabel, exact: true });
  await begin.focus(); await page.keyboard.press('Enter');
  await expect(begin).toBeFocused(); await expect(begin).toHaveAttribute('aria-busy', 'true');
  await expect(begin).toHaveAccessibleName(fixtures.busyLabel);
  await page.keyboard.press('Enter'); await page.keyboard.press('Space');
  await begin.dispatchEvent('click');
  for (const variant of fixtures.variants) {
    const buttons = page.locator(`[data-variant="${variant}"] button`);
    await expect(buttons.nth(1)).toBeDisabled();
    await expect(buttons.nth(1)).toHaveAccessibleDescription(fixtures.disabledReason);
    await buttons.nth(1).dispatchEvent('click');
    await buttons.nth(2).dispatchEvent('click');
    await expect(buttons.nth(2)).toHaveAccessibleDescription(fixtures.busyReason);
  }
  await expect(page.getByRole('status', { name: fixtures.invocationLabel })).toHaveText('1');
});

test('all button variants preserve default hover pressed disabled and loading styles and actual indicator geometry', async ({ page }, info) => {
  await open(page, info);
  const colors = async (button: ReturnType<Page['locator']>) => button.evaluate(node => {
    const style = getComputedStyle(node); return { background: style.backgroundColor, text: style.color };
  });
  let events = 0;
  for (const variant of fixtures.variants) {
    const buttons = page.locator(`[data-variant="${variant}"] button`);
    const active = buttons.nth(0), disabled = buttons.nth(1), loading = buttons.nth(2);
    const normal = { background: variant === 'primary' ? 'rgb(36, 91, 238)' : 'rgb(255, 255, 255)',
      text: variant === 'primary' ? 'rgb(255, 255, 255)' : 'rgb(24, 34, 55)' };
    await active.scrollIntoViewIfNeeded(); await page.mouse.move(0, 0);
    expect(await colors(active)).toEqual(normal);
    const bounds = await active.boundingBox(); expect(bounds).not.toBeNull();
    expect(bounds!.height).toBe(variant === 'secondary' ? 38 : 36);
    await active.hover();
    const hovered = { ...normal, background: variant === 'primary' ? 'rgb(25, 68, 186)' : normal.background };
    expect(await colors(active)).toEqual(hovered); expect(await active.boundingBox()).toEqual(bounds);
    await page.mouse.down();
    try {
      expect(await active.evaluate(node => node.matches(':active'))).toBe(true);
      expect(await colors(active)).toEqual(hovered); expect(await active.boundingBox()).toEqual(bounds);
    } finally { await page.mouse.up(); }
    events++;
    await expect(page.getByRole('status', { name: fixtures.invocationLabel })).toHaveText(String(events));
    await expect(disabled).toBeDisabled(); await expect(disabled).toHaveAccessibleName(fixtures.buttonLabel);
    expect(await colors(disabled)).toEqual({ background: 'rgb(237, 240, 245)', text: 'rgb(98, 111, 132)' });
    await disabled.scrollIntoViewIfNeeded(); const disabledBounds = await disabled.boundingBox();
    expect(disabledBounds!.height).toBe(bounds!.height); expect(disabledBounds!.width).toBe(bounds!.width);
    await page.mouse.click(disabledBounds!.x + disabledBounds!.width / 2, disabledBounds!.y + disabledBounds!.height / 2);
    await loading.scrollIntoViewIfNeeded();
    await expect(loading).toHaveAccessibleName(fixtures.buttonLabel); await expect(loading).toHaveAttribute('aria-busy', 'true');
    expect(await colors(loading)).toEqual(normal);
    const loadingBounds = await loading.boundingBox(); const indicator = loading.locator('img');
    const imageBounds = await indicator.boundingBox();
    expect(imageBounds!.width).toBe(12); expect(imageBounds!.height).toBe(12);
    expect(loadingBounds!.height).toBe(bounds!.height); expect(loadingBounds!.width - bounds!.width).toBe(18);
    expect(imageBounds!.y + 6).toBe(loadingBounds!.y + loadingBounds!.height / 2);
    expect(await indicator.evaluate(node => {
      const image = node as HTMLImageElement; return [image.complete, image.naturalWidth, image.naturalHeight, image.alt];
    })).toEqual([true, 12, 12, '']);
    await page.mouse.click(loadingBounds!.x + loadingBounds!.width / 2, loadingBounds!.y + loadingBounds!.height / 2);
    await expect(page.getByRole('status', { name: fixtures.invocationLabel })).toHaveText(String(events));
  }
});

test('coarse pointer presentation provides at least 44px button targets', async ({ browser, baseURL }, info) => {
  const context = await browser.newContext({ hasTouch: true, isMobile: true, viewport: { width: 320, height: 800 },
    baseURL, deviceScaleFactor: 1, locale: 'en-US', timezoneId: 'UTC', colorScheme: 'light', reducedMotion: 'reduce', serviceWorkers: 'block' });
  const page = await context.newPage();
  try {
    await installBoundary(page, baseURL); await open(page, info, 320);
    expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true);
    for (const button of await page.getByRole('button').all()) {
      const bounds = await button.boundingBox(); expect(bounds!.height).toBeGreaterThanOrEqual(44);
      expect(bounds!.width).toBeGreaterThanOrEqual(44);
    }
    expect(unexpected.get(page)).toEqual([]);
  } finally { await context.close(); }
});

test('manual tabs move focus without activation, skip disabled tabs and associate only the selected panel', async ({ page }, info) => {
  await open(page, info);
  const overview = page.getByRole('tab', { name: 'Overview', exact: true });
  const details = page.getByRole('tab', { name: 'Details', exact: true });
  const notes = page.getByRole('tab', { name: 'Notes', exact: true });
  await overview.focus(); await page.keyboard.press('ArrowRight');
  await expect(details).toBeFocused(); await expect(overview).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('tabpanel')).toHaveText(fixtures.tabs[0].content);
  await page.keyboard.press('End'); await expect(notes).toBeFocused();
  await page.keyboard.press('Home'); await expect(overview).toBeFocused();
  await page.keyboard.press('ArrowLeft'); await expect(notes).toBeFocused();
  await page.keyboard.press('Space'); await expect(notes).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('tabpanel')).toHaveAccessibleName('Notes');
  await expect(page.getByRole('tabpanel')).toHaveText(fixtures.tabs[3].content);
  await page.keyboard.press('ArrowLeft'); await page.keyboard.press('Enter');
  await expect(details).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('tabpanel')).toHaveText(fixtures.tabs[2].content);
});

test('200 percent text and long labels reflow without clipping or horizontal overflow', async ({ page }, info) => {
  await open(page, info, 320);
  await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
  const button = page.getByRole('button', { name: fixtures.longLabel, exact: true });
  await button.scrollIntoViewIfNeeded(); await expect(button).toBeInViewport();
  expect(await button.evaluate(node => node.scrollWidth <= node.clientWidth && node.scrollHeight <= node.clientHeight)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  for (const tab of await page.getByRole('tab').all()) {
    expect(await tab.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  }
});

test('leaving an unselected focused tab restores the active tab as the keyboard re-entry point', async ({ page }, info) => {
  await open(page, info);
  const overview = page.getByRole('tab', { name: 'Overview', exact: true });
  const details = page.getByRole('tab', { name: 'Details', exact: true });
  await overview.focus(); await page.keyboard.press('ArrowRight'); await expect(details).toBeFocused();
  await expect(overview).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('Tab'); await expect(page.getByRole('tabpanel')).toBeFocused();
  await page.keyboard.press('Shift+Tab'); await expect(overview).toBeFocused();
  await expect(overview).toHaveAttribute('aria-selected', 'true');
  await expect(details).toHaveAttribute('aria-selected', 'false');
});

test('an external controlled selection updates keyboard re-entry without stealing outside focus', async ({ page }, info) => {
  await open(page, info);
  await page.getByRole('tab', { name: 'Overview', exact: true }).focus();
  await page.keyboard.press('ArrowRight'); await page.keyboard.press('Tab');
  const control = page.getByRole('button', { name: fixtures.selectNotesLabel, exact: true });
  await control.click(); await expect(control).toBeFocused();
  const notes = page.getByRole('tab', { name: 'Notes', exact: true });
  await expect(notes).toHaveAttribute('aria-selected', 'true');
  await expect(notes).toHaveAttribute('tabindex', '0');
  await page.keyboard.press('Shift+Tab'); await expect(page.getByRole('tabpanel')).toBeFocused();
  await page.keyboard.press('Shift+Tab'); await expect(notes).toBeFocused();
});

for (const state of ['disabled', 'removed'] as const) {
  test(`a ${state} former roving tab cannot remain the keyboard entry point`, async ({ page }, info) => {
    await open(page, info);
    const overview = page.getByRole('tab', { name: 'Overview', exact: true });
    await overview.focus(); await page.keyboard.press('ArrowRight');
    await expect(page.getByRole('tab', { name: 'Details', exact: true })).toBeFocused();
    const label = state === 'disabled' ? fixtures.disableDetailsLabel : fixtures.removeDetailsLabel;
    await page.getByRole('button', { name: label, exact: true }).click();
    if (state === 'disabled') await expect(page.getByRole('tab', { name: 'Details', exact: true })).toBeDisabled();
    else await expect(page.getByRole('tab', { name: 'Details', exact: true })).toHaveCount(0);
    await expect(page.locator('[role="tab"][tabindex="0"]')).toHaveCount(1);
    await expect(overview).toHaveAttribute('tabindex', '0');
    await page.getByRole('tabpanel').focus(); await page.keyboard.press('Shift+Tab');
    await expect(overview).toBeFocused(); await expect(overview).toHaveAttribute('aria-selected', 'true');
  });
}

test('forced colors retains a visible keyboard outline without animations', async ({ page }, info) => {
  await page.emulateMedia({ forcedColors: 'active', reducedMotion: 'reduce' });
  await open(page, info, 768);
  await page.keyboard.press('Tab');
  const button = page.locator('[data-variant="primary"] button').first(); await expect(button).toBeFocused();
  expect(await button.evaluate(node => {
    const style=getComputedStyle(node); return [style.outlineWidth, style.outlineOffset, style.outlineStyle];
  })).toEqual(['2px', '2px', 'solid']);
  const systemColors = await button.evaluate(node => {
    const sample = document.createElement('span'); sample.style.color = 'Highlight';
    document.body.append(sample);
    try { return { outline: getComputedStyle(node).outlineColor, highlight: getComputedStyle(sample).color }; }
    finally { sample.remove(); }
  });
  expect(systemColors.outline).toBe(systemColors.highlight);
  expect(await page.locator('.ot-button-indicator').first().evaluate(node => getComputedStyle(node).animationName)).toBe('none');
  const bounds = await button.boundingBox();
  await capture(page, info, 'focus-forced-colors', { x: bounds!.x - 6, y: bounds!.y - 6, width: bounds!.width + 12, height: bounds!.height + 12 });
});

test('fresh generated tenant presentation resets local state and makes no backend or external requests', async ({ page }, info) => {
  await open(page, info, 768, 'a');
  await page.getByRole('button', { name: fixtures.busyLabel, exact: true }).click();
  await expect(page.getByRole('status', { name: fixtures.invocationLabel })).toHaveText('1');
  await open(page, info, 768, 'b');
  await expect(page.getByText(`${fixtures.tenants[1]} · ${info.project.name}`, { exact: true })).toBeVisible();
  await expect(page.getByText(new RegExp(fixtures.tenants[0]))).toHaveCount(0);
  await expect(page.getByRole('status', { name: fixtures.invocationLabel })).toHaveText('0');
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length, cookies: document.cookie }))).toEqual({ local: 0, session: 0, cookies: '' });
});

type OverlayHistory = { focus: string[]; checkpoint: number; closes: number; cancels: number };
const overlayHistories = new WeakMap<Page, OverlayHistory[]>();
test.afterEach(async ({ page }, info) => {
  if (page.isClosed()) return;
  const observed = await page.evaluate(() => (window as unknown as { overlayHistory?: OverlayHistory }).overlayHistory);
  if (observed) await writeFile(info.outputPath('overlay-focus-history.json'), JSON.stringify({
    test: info.title, mode: info.project.name, status: info.status,
    observations: [...(overlayHistories.get(page) ?? []), observed],
    consumer_focus_return: info.title.includes('B19') ? 'UNAVAILABLE_TARGETS_CONSUMER_NOT_ACCEPTED' : 'SEE_TEST_ASSERTIONS',
    provider_qualification: 'NOT_RUN',
  }));
});
async function openOverlay(page: Page, info: TestInfo, kind: 'dialog' | 'drawer', options: Record<string, string> = {}, width = 1280,
  beforeOpening?: () => Promise<void>) {
  const height = new Map([[320, 800], [768, 1024], [1280, 900], [1440, 1024]]).get(width)!;
  await page.setViewportSize({ width, height });
  const previous = await page.evaluate(() => (window as unknown as { overlayHistory?: OverlayHistory }).overlayHistory);
  if (previous) overlayHistories.set(page, [...(overlayHistories.get(page) ?? []), previous]);
  await page.addInitScript(() => {
    if ('overlayHistory' in window) return;
    const history: OverlayHistory = { focus: [], checkpoint: 0, closes: 0, cancels: 0 };
    Object.assign(window, { overlayHistory: history });
    document.addEventListener('focusin', event => history.focus.push((event.target as HTMLElement).textContent ?? ''), true);
    document.addEventListener('close', () => history.closes++, true);
    document.addEventListener('cancel', () => history.cancels++, true);
  });
  await page.goto(`/?${new URLSearchParams({ mode: info.project.name, overlay: kind, ...options })}`);
  await expect(page.getByRole('heading', { name: 'Generated overlay examples' })).toBeVisible();
  await page.evaluate(async () => {
    await Promise.all([document.fonts.load('400 14px Geist'), document.fonts.load('500 14px Geist'), document.fonts.load('600 18px Geist')]);
    await document.fonts.ready;
  });
  await beforeOpening?.();
  await page.getByRole('button', { name: 'Open generated overlay', exact: true }).click();
  await expect(page.locator('dialog:modal')).toHaveCount(1);
}
async function history(page: Page, clear = false) {
  return page.evaluate(reset => {
    const value = (window as unknown as { overlayHistory: OverlayHistory }).overlayHistory;
    const copy = { ...value, focus: value.focus.slice(value.checkpoint) };
    if (reset) value.checkpoint = value.focus.length; return copy;
  }, clear);
}
async function insideModal(page: Page) {
  expect(await page.evaluate(() => !!document.querySelector('dialog:modal')?.contains(document.activeElement))).toBe(true);
}
/** Computed colours serialise as rgb()/rgba() or color(srgb ...); compare the 8-bit channels and alpha. */
function channels(value: string): number[] {
  const numbers = (value.match(/-?\d*\.?\d+(?:e-?\d+)?/g) ?? []).map(Number);
  if (value.startsWith('color(srgb ') && numbers.length >= 3) return [...numbers.slice(0, 3).map(part => Math.round(part * 255)), numbers[3] ?? 1];
  if (/^rgba?\(/.test(value) && numbers.length >= 3) return [...numbers.slice(0, 3), numbers[3] ?? 1];
  throw new Error(`Unrecognised computed colour ${value}`);
}
const backdropColor = (page: Page) => page.locator('dialog:modal').evaluate(node => getComputedStyle(node, '::backdrop').backgroundColor);
/** Rendered pixels of a 2px ring at 2px offset, sampled at the midpoint of every side. */
async function renderedRing(page: Page, selector: string, sides: ReadonlyArray<'left' | 'right' | 'top' | 'bottom'> = ['left', 'right', 'top', 'bottom']) {
  const box = await page.locator(selector).evaluate(node => { const rect = node.getBoundingClientRect(); return { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom }; });
  for (const side of sides) expect(Number.isInteger(box[side])).toBe(true);
  const middleX = Math.floor((box.left + box.right) / 2); const middleY = Math.floor((box.top + box.bottom) / 2);
  const raster = decodePng(await page.screenshot({ animations: 'disabled', caret: 'hide' }));
  const pixel = (x: number, y: number) => [...raster.data.subarray((y * raster.width + x) * 4, (y * raster.width + x) * 4 + 3)].join();
  const samples = {
    left: [pixel(box.left - 3, middleY), pixel(box.left - 4, middleY)], right: [pixel(box.right + 2, middleY), pixel(box.right + 3, middleY)],
    top: [pixel(middleX, box.top - 3), pixel(middleX, box.top - 4)], bottom: [pixel(middleX, box.bottom + 2), pixel(middleX, box.bottom + 3)],
  };
  return Object.fromEntries(sides.map(side => [side, samples[side]]));
}
const fullRing = { left: ['36,91,238', '36,91,238'], right: ['36,91,238', '36,91,238'], top: ['36,91,238', '36,91,238'], bottom: ['36,91,238', '36,91,238'] };
const settled = (page: Page) => page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));

for (const kind of ['dialog', 'drawer'] as const) {
  const closeName = kind === 'dialog' ? 'Close dialog' : 'Close details';
  test(`overlay ${kind} B01 B02 owned safer Close receives focus and returns after caller dismissal`, async ({ page }, info) => {
    await openOverlay(page, info, kind);
    await expect(page.getByRole('button', { name: closeName, exact: true })).toBeFocused();
    expect(await page.getByRole('button', { name: closeName, exact: true }).evaluate(node => {
      const style = getComputedStyle(node); return [style.outlineWidth, style.outlineOffset, style.outlineColor];
    })).toEqual(['2px', '2px', 'rgb(36, 91, 238)']);
    await page.keyboard.press('Escape');
    await expect(page.locator('dialog')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Open generated overlay', exact: true })).toBeFocused();
    await expect(page.getByRole('status', { name: 'Dismissal requests' })).toHaveText('escape');
    expect((await history(page)).closes).toBe(0);
  });
  test(`overlay ${kind} B03 long structured content starts at its focused heading`, async ({ page }, info) => {
    await openOverlay(page, info, kind, { content: 'long' }, 320);
    await expect(page.locator('.ot-modal-title')).toBeFocused();
    expect(await page.locator('.ot-modal-body').evaluate(node => node.scrollTop)).toBe(0);
    await expect(page.getByRole('heading', { name: 'About this example' })).toBeInViewport();
  });
  test(`overlay ${kind} B04 keyboard cycles within modal and native inertness blocks background focus`, async ({ page }, info) => {
    await openOverlay(page, info, kind, { probe: '1', ignore: '1' });
    for (let index = 0; index < 18; index++) { await page.keyboard.press('Tab'); await insideModal(page); }
    for (let index = 0; index < 18; index++) { await page.keyboard.press('Shift+Tab'); await insideModal(page); }
    await page.getByRole('button', { name: 'Safe current fallback', includeHidden: true }).evaluate(node => node.focus());
    await insideModal(page);
    const background = await page.getByRole('button', { name: 'Open generated overlay', includeHidden: true, exact: true }).boundingBox();
    await page.mouse.click(background!.x + 2, background!.y + 2);
    await expect(page.locator('dialog:modal')).toHaveCount(1);
    await expect(page.getByRole('status', { name: 'Dismissal requests', includeHidden: true })).toHaveText('');
    await expect(page.getByRole('status', { name: 'Generated opening events', includeHidden: true })).toHaveText('1');
    await page.locator('.ot-modal-body').evaluate(node => {
      const controls = document.createElement('div');
      controls.innerHTML = '<button type="button">Before native controls</button>' +
        '<details><summary>Generated summary</summary></details>' +
        '<div role="textbox" aria-label="Implicit editor" contenteditable=""></div>' +
        '<div role="textbox" aria-label="Explicit editor" contenteditable="true"></div>' +
        '<button type="button">After native controls</button>';
      node.append(controls);
    });
    const before = page.getByRole('button', { name: 'Before native controls' });
    const after = page.getByRole('button', { name: 'After native controls' });
    const nativeControls = [
      page.locator('dialog summary'),
      page.getByRole('textbox', { name: 'Implicit editor' }),
      page.getByRole('textbox', { name: 'Explicit editor' }),
    ];
    await before.focus();
    for (const control of [...nativeControls, after]) {
      await page.keyboard.press('Tab');
      await expect(control).toBeFocused();
    }
    for (const control of [...nativeControls].reverse().concat(before)) {
      await page.keyboard.press('Shift+Tab');
      await expect(control).toBeFocused();
    }
  });
  test(`overlay ${kind} B05 B20 repeated ignored real Escape cannot trigger native close`, async ({ page }, info) => {
    await openOverlay(page, info, kind, { ignore: '1' });
    for (let index = 1; index <= 6; index++) {
      await page.keyboard.press('Escape');
      await expect(page.locator('dialog:modal')).toHaveCount(1); await insideModal(page);
      await expect(page.getByRole('status', { name: 'Dismissal requests', includeHidden: true })).toHaveText(Array(index).fill('escape').join(','));
    }
    expect((await history(page)).closes).toBe(0);
  });
  test(`overlay ${kind} B06 ignored explicit Close remains request-only`, async ({ page }, info) => {
    await openOverlay(page, info, kind, { ignore: '1' });
    await page.getByRole('button', { name: closeName, exact: true }).click();
    await expect(page.locator('dialog:modal')).toHaveCount(1); await insideModal(page);
    await expect(page.getByRole('status', { name: 'Dismissal requests', includeHidden: true })).toHaveText('close-button');
  });
  test(`overlay ${kind} B07 B08 backdrop does nothing and Enter closes without form submission`, async ({ page }, info) => {
    await openOverlay(page, info, kind);
    await page.mouse.click(2, 2);
    await expect(page.locator('dialog:modal')).toHaveCount(1);
    await expect(page.getByRole('status', { name: 'Dismissal requests', includeHidden: true })).toHaveText('');
    await page.getByRole('button', { name: closeName, exact: true }).focus(); await page.keyboard.press('Enter');
    await expect(page.locator('dialog')).toHaveCount(0);
    await expect(page.getByRole('status', { name: 'Overlay action events' })).toHaveText('0');
    await page.getByRole('button', { name: 'Safe current fallback' }).focus();
    await expect(page.getByRole('button', { name: 'Safe current fallback' })).toBeFocused();
  });
  test(`overlay ${kind} B09 supplied action invokes only its explicit callback`, async ({ page }, info) => {
    await openOverlay(page, info, kind, { probe: '1', focus: 'element' });
    const action = page.getByRole('button', { name: 'Example action', exact: true });
    await expect(action).toBeFocused(); await page.keyboard.press('Enter');
    await expect(page.getByRole('status', { name: 'Overlay action events', includeHidden: true })).toHaveText('1');
    await expect(page.getByRole('status', { name: 'Dismissal requests', includeHidden: true })).toHaveText('');
    await expect(page.locator('dialog:modal')).toHaveCount(1);
  });
  test(`overlay ${kind} B10 rerender preserves current focus and scroll`, async ({ page }, info) => {
    await openOverlay(page, info, kind, { probe: '1', content: 'long' }, 320);
    const control = page.getByRole('button', { name: 'Rerender generated content' });
    await control.click(); const position = await page.locator('.ot-modal-body').evaluate(node => node.scrollTop);
    await page.keyboard.press('Enter'); await expect(control).toBeFocused();
    expect(await page.locator('.ot-modal-body').evaluate(node => node.scrollTop)).toBe(position);
    await expect(page.getByRole('status', { name: 'Generated render count', includeHidden: true })).toHaveText('2');
  });
  test(`overlay ${kind} B11 removed or disabled origins use the explicit fallback without transient unsafe focus`, async ({ page }, info) => {
    for (const label of ['Remove origin', 'Disable origin']) {
      await openOverlay(page, info, kind, { probe: '1' });
      await page.getByRole('button', { name: label, exact: true }).click(); await history(page, true);
      await page.getByRole('button', { name: closeName, exact: true }).click();
      await expect(page.getByRole('button', { name: 'Safe current fallback' })).toBeFocused();
      expect((await history(page)).focus).not.toContain('Open generated overlay');
    }
  });
  test(`overlay ${kind} B12 close-time local context resolver excludes the still-connected old origin`, async ({ page }, info) => {
    await openOverlay(page, info, kind, { probe: '1' });
    await page.getByRole('button', { name: 'Change local context' }).click(); await history(page, true);
    await page.getByRole('button', { name: closeName, exact: true }).click();
    await expect(page.getByRole('button', { name: 'Current context target' })).toBeFocused();
    expect((await history(page)).focus).not.toContain('Open generated overlay');
  });
  test(`overlay ${kind} B13 invalid initial element falls back to the visible heading`, async ({ page }, info) => {
    for (const focus of ['invalid', 'outside', 'disabled']) {
      await openOverlay(page, info, kind, { focus, probe: '1' }); await expect(page.locator('.ot-modal-title')).toBeFocused();
      await expect(page.getByRole('status', { name: 'Overlay action events', includeHidden: true })).toHaveText('0');
    }
  });
  test(`overlay ${kind} B14 B21 StrictMode replacement and unmount never restore an obsolete origin`, async ({ page }, info) => {
    await openOverlay(page, info, kind, { probe: '1', strict: '1' }); await history(page, true);
    await page.getByRole('button', { name: 'Replace open instance' }).click();
    await expect(page.getByText('Generated instance 1', { exact: true })).toHaveCount(1);
    await expect(page.getByText('Generated instance 0', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: closeName, exact: true })).toBeFocused();
    await insideModal(page); expect((await history(page)).focus).not.toContain('Open generated overlay');
    expect((await history(page)).focus).not.toContain('Current context target');
    await page.getByRole('button', { name: 'Unmount overlay' }).click();
    await expect(page.locator('dialog')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Current context target' })).toBeFocused();
    await page.getByRole('button', { name: 'Reopen generated overlay' }).click();
    await expect(page.getByText('Generated instance 1', { exact: true })).toHaveCount(1);
    await expect(page.getByText('Generated instance 0', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: closeName, exact: true })).toBeFocused();
    await page.keyboard.press('Escape'); await expect(page.locator('dialog')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Current context target' })).toBeFocused();
  });
  for (const width of [320, 768, 1280, 1440]) {
    test(`overlay ${kind} B15 geometry and zero-tolerance design at ${width}px`, async ({ page }, info) => {
      await openOverlay(page, info, kind, width === 320 ? { content: 'long' } : {}, width);
      const panel = page.locator('dialog'); const bounds = (await panel.boundingBox())!;
      const viewport = page.viewportSize()!;
      expect(bounds.width).toBe(Math.min(576, width - (kind === 'dialog' ? 32 : 0)));
      if (kind === 'drawer') { expect(bounds.height).toBe(viewport.height); expect(bounds.x + bounds.width).toBe(width); expect(bounds.y).toBe(0); }
      else {
        expect(bounds.y).toBeGreaterThanOrEqual(16); expect(bounds.y + bounds.height).toBeLessThanOrEqual(viewport.height - 16);
        expect(bounds.x).toBe((viewport.width - bounds.width) / 2);
        expect(bounds.y).toBe((viewport.height - bounds.height) / 2);
      }
      expect(await panel.evaluate(node => {
        const style = getComputedStyle(node); return [style.padding, style.gap, style.boxShadow, style.animationName, style.transitionDuration];
      })).toEqual(['24px', '16px', 'none', 'none', '0s']);
      expect(channels(await backdropColor(page))).toEqual([24, 34, 55, 0.32]);
      // Approved content width; the scroller itself is 4px wider on every side so focus rings are not clipped.
      expect(await page.locator('.ot-modal-body').evaluate(node => {
        const style = getComputedStyle(node); const rect = node.getBoundingClientRect();
        return [rect.width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight), style.paddingLeft, style.paddingRight, rect.x];
      })).toEqual([bounds.width - 48, '4px', '4px', bounds.x + 20]);
      await capture(page, info, `overlay-${kind}-${width}`);
    });
  }
  test(`overlay ${kind} B16 long content and fixed Close remain reachable at every required viewport`, async ({ page }, info) => {
    for (const width of [320, 768, 1280, 1440]) {
      await openOverlay(page, info, kind, { content: 'long' }, width);
      await expect(page.getByRole('button', { name: closeName, exact: true })).toBeInViewport();
      await page.locator('.ot-modal-body').evaluate(node => { node.scrollTop = node.scrollHeight; });
      await expect(page.getByRole('heading', { name: 'End of generated content' })).toBeInViewport();
      await expect(page.getByRole('button', { name: closeName, exact: true })).toBeInViewport();
    }
  });
  test(`overlay ${kind} B17 doubled text reflows without horizontal clipping or lost Close`, async ({ page }, info) => {
    for (const width of [320, 768, 1280, 1440]) {
      await openOverlay(page, info, kind, { content: 'long' }, width);
      await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
      expect(await page.locator('dialog').evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
      expect(await page.locator('.ot-modal-body').evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
      await expect(page.getByRole('button', { name: closeName, exact: true })).toBeInViewport();
      await page.locator('.ot-modal-body').evaluate(node => { node.scrollTop = node.scrollHeight; });
      await expect(page.getByRole('heading', { name: 'End of generated content' })).toBeInViewport();
    }
  });
  test(`overlay ${kind} B18 forced colors keeps Highlight focus with immediate reduced motion`, async ({ page }, info) => {
    await page.emulateMedia({ forcedColors: 'active', reducedMotion: 'reduce' });
    await openOverlay(page, info, kind, { focus: 'heading' }, 320);
    expect(await page.locator('.ot-modal-title').evaluate(node => {
      const sample = document.createElement('span'); sample.style.color = 'Highlight'; document.body.append(sample);
      try { const style = getComputedStyle(node); return style.outlineColor === getComputedStyle(sample).color && style.outlineWidth === '2px' && style.outlineOffset === '2px'; }
      finally { sample.remove(); }
    })).toBe(true);
    expect(await page.locator('dialog').evaluate(node => getComputedStyle(node).animationName)).toBe('none');
  });
  test(`overlay ${kind} B18 coarse-pointer close and caller action retain at least 44px targets`, async ({ browser, baseURL }, info) => {
    const context = await browser.newContext({ hasTouch: true, baseURL }); const page = await context.newPage();
    try {
      await installBoundary(page, baseURL); await openOverlay(page, info, kind, { probe: '1' }, 320);
      expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true);
      for (const button of await page.locator('dialog button').all()) {
        const bounds = (await button.boundingBox())!;
        expect(bounds.height).toBeGreaterThanOrEqual(44); expect(bounds.width).toBeGreaterThanOrEqual(44);
      }
      expect(unexpected.get(page)).toEqual([]);
    } finally { await context.close(); }
  });
  test(`overlay ${kind} B19 missing both safe return targets still cleans up without claiming focus-return success`, async ({ page }, info) => {
    await openOverlay(page, info, kind, { probe: '1' });
    await page.getByRole('button', { name: 'Remove both return targets' }).click(); await history(page, true);
    await page.getByRole('button', { name: closeName, exact: true }).click();
    await expect(page.locator('dialog')).toHaveCount(0);
    expect((await history(page)).focus).not.toContain('Open generated overlay');
    expect(await page.evaluate(() => document.activeElement === document.body)).toBe(true);
    await page.getByRole('button', { name: 'Current context target' }).focus();
    await expect(page.getByRole('button', { name: 'Current context target' })).toBeFocused();
    await expect(page.getByRole('status', { name: 'Overlay action events' })).toHaveText('0');
  });
  test(`overlay ${kind} F1 overflowing content is a named keyboard stop that real keys scroll at 320px`, async ({ page }, info) => {
    await openOverlay(page, info, kind, { content: 'long' }, 320);
    const body = page.locator('.ot-modal-body'); const title = page.locator('.ot-modal-title');
    const close = page.getByRole('button', { name: closeName, exact: true });
    const position = () => body.evaluate(node => node.scrollTop);
    // Initial focus is unchanged: the heading, with the beginning visible.
    await expect(title).toBeFocused(); expect(await position()).toBe(0);
    expect(await body.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true);
    await page.keyboard.press('Tab');
    await expect(body).toBeFocused();
    await expect(page.getByRole('group', { name: kind === 'dialog' ? overlays.dialogTitle : overlays.drawerTitle, exact: true })).toBeFocused();
    expect(await body.evaluate(node => [node.getAttribute('tabindex'), node.getAttribute('aria-labelledby') === node.closest('dialog')!.getAttribute('aria-labelledby')])).toEqual(['0', true]);
    expect(await body.evaluate(node => { const style = getComputedStyle(node); return [style.outlineStyle, style.outlineWidth, style.outlineOffset, style.outlineColor]; }))
      .toEqual(['solid', '2px', '2px', 'rgb(36, 91, 238)']);
    expect(await renderedRing(page, '.ot-modal-body')).toEqual(fullRing);
    // Chromium can coalesce a subsequent key into an unfinished native scroll.
    const scrollWithKey = async (key: 'ArrowDown' | 'PageDown', start: number) => {
      const completion = await body.evaluateHandle(node => ({
        finished: new Promise<void>(resolve => node.addEventListener('scrollend', () => resolve(), { once: true })),
      }));
      await page.keyboard.press(key);
      await expect.poll(position).toBeGreaterThan(start);
      await completion.evaluate(scroll => scroll.finished);
      await completion.dispose();
    };
    await scrollWithKey('ArrowDown', 0);
    const afterArrow = await position();
    await scrollWithKey('PageDown', afterArrow);
    await page.keyboard.press('End');
    await expect.poll(() => body.evaluate(node => Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop) < 1)).toBe(true);
    await expect(page.getByRole('heading', { name: 'End of generated content' })).toBeInViewport();
    await expect(close).toBeInViewport(); await insideModal(page);
    await page.keyboard.press('Home'); await expect.poll(position).toBe(0);
    // One stop among the others, in both directions.
    await page.keyboard.press('Tab'); await expect(close).toBeFocused();
    await page.keyboard.press('Tab'); await expect(body).toBeFocused();
    await page.keyboard.press('Shift+Tab'); await expect(close).toBeFocused();
    await page.keyboard.press('Shift+Tab'); await expect(body).toBeFocused();
    await expect(page.getByRole('status', { name: 'Dismissal requests', includeHidden: true })).toHaveText('');
    // Content that fits is not a stop and carries no group semantics; it becomes one only when it overflows.
    await openOverlay(page, info, kind, { content: 'long' }, 1440);
    expect(await body.evaluate(node => [node.scrollHeight > node.clientHeight, node.getAttribute('tabindex'), node.getAttribute('role'), node.getAttribute('aria-labelledby')]))
      .toEqual([false, null, null, null]);
    await page.keyboard.press('Tab'); await expect(close).toBeFocused();
    await page.keyboard.press('Tab'); await expect(close).toBeFocused();
    const boxWidth = await body.evaluate(node => node.clientWidth);
    await body.evaluate(node => {
      const fixed = document.createElement('div');
      fixed.style.cssText = 'width:100%;height:44px;flex:none';
      const nested = document.createElement('div');
      nested.id = 'generated-overflow-probe';
      nested.style.cssText = 'width:20px;height:20px';
      fixed.append(nested);
      node.append(fixed);
    });
    const nested = page.locator('#generated-overflow-probe');
    await nested.evaluate(node => { node.style.width = '1600px'; });
    await expect.poll(() => body.evaluate(node => [node.clientWidth, node.scrollWidth > node.clientWidth, node.getAttribute('tabindex'), node.getAttribute('role')]))
      .toEqual([boxWidth, true, '0', 'group']);
    await nested.evaluate(node => { node.style.width = '20px'; });
    await expect.poll(() => body.evaluate(node => [node.scrollWidth > node.clientWidth, node.getAttribute('tabindex'), node.getAttribute('role')]))
      .toEqual([false, null, null]);
    await nested.evaluate(node => node.parentElement!.remove());
    await page.setViewportSize({ width: 320, height: 800 });
    await expect.poll(() => body.evaluate(node => [node.scrollHeight > node.clientHeight, node.getAttribute('tabindex'), node.getAttribute('role')])).toEqual([true, '0', 'group']);
    await page.keyboard.press('Tab'); await expect(body).toBeFocused();
  });
  test(`overlay ${kind} F3 a second modal presentation is refused while the first keeps its modality`, async ({ page }, info) => {
    await openOverlay(page, info, kind, { review: '1', ignore: '1' });
    await page.getByRole('button', { name: 'Open nested overlay', exact: true }).click();
    await expect(page.getByRole('status', { name: 'Presentation error', includeHidden: true })).toHaveText('Nested modal presentation is not supported.');
    await expect(page.locator('dialog')).toHaveCount(1); await expect(page.locator('dialog:modal')).toHaveCount(1);
    await expect(page.getByText('Nested generated overlay', { exact: true })).toHaveCount(0);
    await insideModal(page);
    await page.keyboard.press('Escape');
    await expect(page.getByRole('status', { name: 'Dismissal requests', includeHidden: true })).toHaveText('escape');
    await expect(page.locator('dialog:modal')).toHaveCount(1);
  });
  test(`overlay ${kind} F3 fullscreen presentation is not mistaken for another modal`, async ({ page }, info) => {
    await openOverlay(page, info, kind, {}, 1280, async () => {
      await page.getByRole('heading', { name: 'Generated overlay examples' }).click();
      await page.evaluate(() => document.documentElement.requestFullscreen());
      expect(await page.evaluate(() => [document.fullscreenElement === document.documentElement, document.documentElement.matches(':modal')])).toEqual([true, true]);
    });
    await expect(page.getByRole('button', { name: closeName, exact: true })).toBeFocused();
    await expect(page.getByRole('heading', { name: 'Generated overlay examples', includeHidden: true })).toHaveCount(1);
    await page.getByRole('button', { name: closeName, exact: true }).click();
    await expect(page.locator('dialog')).toHaveCount(0);
    expect(await page.evaluate(() => document.fullscreenElement === document.documentElement)).toBe(true);
    await expect(page.getByRole('button', { name: 'Open generated overlay', exact: true })).toBeFocused();
    await expect(page.getByRole('status', { name: 'Dismissal requests' })).toHaveText('close-button');
  });
  test(`overlay ${kind} F4 approved rhythm and metadata type come from the component without a consumer reset`, async ({ page }, info) => {
    const measure = () => page.evaluate(() => {
      const box = (node: Element) => { const rect = node.getBoundingClientRect(); return [rect.x, rect.y, rect.width, rect.height]; };
      const dialog = document.querySelector('dialog:modal')!;
      return { dialog: box(dialog), parts: [...dialog.querySelectorAll('.ot-modal-title, .ot-modal-body > *, .ot-modal-actions')].map(box),
        type: [...dialog.querySelectorAll('.ot-modal-body > *')].map(node => { const style = getComputedStyle(node); return [node.tagName, style.fontSize, style.lineHeight, style.fontWeight, style.marginTop, style.marginBottom, style.color]; }) };
    });
    const withoutConsumerStyles = () => page.evaluate(() => { for (const sheet of document.styleSheets) if (sheet.ownerNode instanceof HTMLStyleElement) sheet.disabled = true; });
    await openOverlay(page, info, kind);
    const short = await measure();
    expect(short.type).toEqual([['P', '14px', '22px', '400', '0px', '0px', 'rgb(81, 96, 120)'], ['P', '12px', '18px', '400', '0px', '0px', 'rgb(81, 96, 120)']]);
    if (kind === 'dialog') expect(short.dialog).toEqual([352, 339, 576, 222]);
    await withoutConsumerStyles();
    expect(await page.evaluate(() => [...document.styleSheets].filter(sheet => !sheet.disabled).length)).toBeGreaterThan(0);
    expect(await measure()).toEqual(short);
    await openOverlay(page, info, kind, { content: 'long' }, 320);
    const long = await measure();
    expect(long.type.slice(0, 2)).toEqual([['H3', '18px', '26px', '600', '0px', '0px', 'rgb(24, 34, 55)'], ['P', '14px', '22px', '400', '0px', '0px', 'rgb(81, 96, 120)']]);
    await withoutConsumerStyles();
    expect(await measure()).toEqual(long);
  });
  test(`overlay ${kind} F5 a body child at the scroller edge shows its whole focus ring`, async ({ page }, info) => {
    await openOverlay(page, info, kind, { edge: '1' });
    const edge = page.getByRole('link', { name: 'Generated edge target', exact: true });
    await page.keyboard.press('Shift+Tab'); await expect(edge).toBeFocused();
    // The child spans the approved content box exactly, so its ring lies wholly outside that box.
    const dialog = (await page.locator('dialog:modal').boundingBox())!; const target = (await edge.boundingBox())!;
    expect([target.x, target.width]).toEqual([dialog.x + 24, dialog.width - 48]);
    expect(await edge.evaluate(node => { const style = getComputedStyle(node); return [style.outlineStyle, style.outlineWidth, style.outlineOffset, style.outlineColor]; }))
      .toEqual(['solid', '2px', '2px', 'rgb(36, 91, 238)']);
    expect(await renderedRing(page, '.overlay-edge')).toEqual(fullRing);
    // Overflowing content: every button the keyboard reaches, including the last one at the end of the scroll range.
    await openOverlay(page, info, kind, { content: 'long', review: '1' }, 320);
    const body = page.locator('.ot-modal-body');
    for (const label of ['Caller closes then focuses its own target', 'Caller unmounts then focuses its own target', 'Open nested overlay']) {
      const control = page.getByRole('button', { name: label, exact: true });
      for (let presses = 0; presses < 8 && !await control.evaluate(node => node === document.activeElement); presses++) await page.keyboard.press('Tab');
      await expect(control).toBeFocused(); await settled(page);
      expect(await renderedRing(page, 'dialog:modal .ot-button:focus', ['left', 'top', 'bottom']))
        .toEqual({ left: fullRing.left, top: fullRing.top, bottom: fullRing.bottom });
    }
    expect(await body.evaluate(node => Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop) < 1)).toBe(true);
  });
  test(`overlay ${kind} F6 each initial focus policy focuses its target exactly once`, async ({ page }, info) => {
    const title = kind === 'dialog' ? overlays.dialogTitle : overlays.drawerTitle;
    for (const [options, expected] of [[{ focus: 'heading' }, title], [{}, closeName], [{ focus: 'element', probe: '1' }, 'Example action']] as const) {
      await openOverlay(page, info, kind, options);
      await settled(page);
      expect((await history(page)).focus).toEqual(['Open generated overlay', expected]);
      expect(await page.evaluate(() => document.activeElement?.textContent)).toBe(expected);
    }
  });
  test(`overlay ${kind} F7 delayed restoration never overrides focus the caller already placed`, async ({ page }, info) => {
    for (const label of ['Caller closes then focuses its own target', 'Caller unmounts then focuses its own target']) {
      await openOverlay(page, info, kind, { review: '1' }); await history(page, true);
      await page.getByRole('button', { name: label, exact: true }).click();
      await expect(page.locator('dialog')).toHaveCount(0);
      await settled(page);
      await expect(page.getByRole('button', { name: 'Current context target' })).toBeFocused();
      expect((await history(page)).focus).toEqual([label, 'Current context target']);
    }
    // Focus that is actually lost is still restored through the resolver.
    await openOverlay(page, info, kind, { review: '1' }); await history(page, true);
    await page.keyboard.press('Escape'); await expect(page.locator('dialog')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Open generated overlay', exact: true })).toBeFocused();
    expect((await history(page)).focus).toEqual(['Open generated overlay']);
  });
  test(`overlay ${kind} F8 backdrop follows the text-primary token at 32 percent`, async ({ page }, info) => {
    await openOverlay(page, info, kind);
    expect(channels(await backdropColor(page))).toEqual([24, 34, 55, 0.32]);
    await page.evaluate(() => document.documentElement.style.setProperty('--ot-text-primary', '#804020'));
    await expect.poll(async () => channels(await backdropColor(page))).toEqual([128, 64, 32, 0.32]);
  });
  test(`overlay ${kind} Escape requests once per key press and defers to content that handled it`, async ({ page }, info) => {
    await openOverlay(page, info, kind, { review: '1', ignore: '1' });
    const requests = page.getByRole('status', { name: 'Dismissal requests', includeHidden: true });
    await page.getByRole('textbox', { name: 'Field that consumes Escape' }).click();
    await page.keyboard.press('Escape');
    await settled(page); await expect(requests).toHaveText(''); await expect(page.locator('dialog:modal')).toHaveCount(1);
    await page.getByRole('textbox', { name: 'Plain generated field' }).click();
    // An Escape that cancels input-method composition is not a dismissal request.
    await page.evaluate(() => document.addEventListener('keydown', event => { if (event.key === 'Escape') Object.assign(window, { escapeWhileComposing: event.isComposing }); }, { capture: true, once: true }));
    const input = await page.context().newCDPSession(page);
    await input.send('Input.imeSetComposition', { text: 'generated', selectionStart: 9, selectionEnd: 9 });
    await page.keyboard.press('Escape');
    expect(await page.evaluate(() => (window as unknown as { escapeWhileComposing?: boolean }).escapeWhileComposing)).toBe(true);
    await settled(page); await expect(requests).toHaveText(''); await expect(page.locator('dialog:modal')).toHaveCount(1);
    await input.send('Input.imeSetComposition', { text: '', selectionStart: 0, selectionEnd: 0 }); await input.detach();
    await page.keyboard.down('Escape'); await page.keyboard.down('Escape'); await page.keyboard.down('Escape'); await page.keyboard.up('Escape');
    await settled(page); await expect(requests).toHaveText('escape'); await expect(page.locator('dialog:modal')).toHaveCount(1);
    await page.keyboard.press('Escape');
    await expect(requests).toHaveText('escape,escape'); await expect(page.locator('dialog:modal')).toHaveCount(1); await insideModal(page);
    const observed = await history(page); expect([observed.closes, observed.cancels]).toEqual([0, 0]);
  });
  if (kind === 'drawer') test('overlay drawer RTL sits on the inline end with its border on the inner edge', async ({ page }, info) => {
    await openOverlay(page, info, kind, { dir: 'rtl' });
    const bounds = (await page.locator('dialog:modal').boundingBox())!;
    expect([bounds.x, bounds.y, bounds.width, bounds.height]).toEqual([0, 0, 576, 900]);
    expect(await page.locator('dialog:modal').evaluate(node => { const style = getComputedStyle(node, '::after'); return [style.borderLeftWidth, style.borderRightWidth]; })).toEqual(['0px', '1px']);
    await openOverlay(page, info, kind);
    const ltr = (await page.locator('dialog:modal').boundingBox())!;
    expect([ltr.x, ltr.width]).toEqual([1280 - 576, 576]);
    expect(await page.locator('dialog:modal').evaluate(node => { const style = getComputedStyle(node, '::after'); return [style.borderLeftWidth, style.borderRightWidth]; })).toEqual(['1px', '0px']);
  });
}
