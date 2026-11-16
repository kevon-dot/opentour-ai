import { test, expect } from '@playwright/test';
import type { Page, TestInfo } from '@playwright/test';
import { createHash } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
import { presentationFixtures as fixtures } from './fixtures';

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
async function openOverlay(page: Page, info: TestInfo, kind: 'dialog' | 'drawer', options: Record<string, string> = {}, width = 1280) {
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
    await expect(page.getByRole('button', { name: closeName, exact: true })).toBeFocused();
    await insideModal(page); expect((await history(page)).focus).not.toContain('Open generated overlay');
    expect((await history(page)).focus).not.toContain('Current context target');
    await page.getByRole('button', { name: 'Unmount overlay' }).click();
    await expect(page.locator('dialog')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Current context target' })).toBeFocused();
    await page.getByRole('button', { name: 'Reopen generated overlay' }).click();
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
        const style = getComputedStyle(node); return [style.padding, style.gap, style.boxShadow, style.animationName, style.transitionDuration, getComputedStyle(node, '::backdrop').backgroundColor];
      })).toEqual(['24px', '16px', 'none', 'none', '0s', 'rgba(24, 34, 55, 0.32)']);
      expect(await page.locator('.ot-modal-body').evaluate(node => node.getBoundingClientRect().width)).toBe(bounds.width - 48);
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
}
