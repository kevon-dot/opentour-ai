import { test, expect } from '@playwright/test';
import type { Page, TestInfo } from '@playwright/test';
import { createHash } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
import { presentationFixtures as fixtures } from './fixtures';

const unexpected = new WeakMap<Page, string[]>();
test.beforeEach(async ({ page, baseURL }) => {
  const denied: string[] = []; unexpected.set(page, denied);
  await page.route('**/*', async route => {
    const request = route.request(); const url = new URL(request.url());
    if (url.origin !== baseURL || request.method() !== 'GET' ||
      !(url.pathname === '/' || url.pathname.startsWith('/assets/'))) {
      denied.push(`${request.method()}:${url.origin}${url.pathname}`); await route.abort(); return;
    }
    await route.continue();
  });
});
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

test('forced colors retains a visible keyboard outline without animations', async ({ page }, info) => {
  await page.emulateMedia({ forcedColors: 'active', reducedMotion: 'reduce' });
  await open(page, info, 768);
  await page.keyboard.press('Tab');
  const button = page.locator('[data-variant="primary"] button').first(); await expect(button).toBeFocused();
  expect(await button.evaluate(node => {
    const style=getComputedStyle(node); return [style.outlineWidth, style.outlineOffset, style.outlineStyle];
  })).toEqual(['2px', '2px', 'solid']);
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
