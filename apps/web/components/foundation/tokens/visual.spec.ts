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
