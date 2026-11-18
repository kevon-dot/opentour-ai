import { expect, test, type Page, type TestInfo } from '@playwright/test';
import { buildInlineFixture } from '../../../tests/fixtures/presentation/build.ts';
import { join } from 'node:path';
import { writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { derivedValueFixtures as fixtures } from './derived-value.fixtures';

let script: string, styles: string;
const observations = new WeakMap<Page, { requests: string[]; errors: string[] }>();
test.beforeAll(async () => {
  const output = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/derived-value.tsx'), 'esm');
  script = output.script; styles = output.css;
});
test.afterEach(async ({ page }) => {
  expect(observations.get(page)?.requests).toEqual([]);
  expect(observations.get(page)?.errors).toEqual([]);
  expect(await page.evaluate(() => [Object.keys(localStorage), Object.keys(sessionStorage)])).toEqual([[], []]);
});

async function open(page: Page, baseURL: string | undefined, mode: string, width = 1280, scale = 100) {
  if (!baseURL) throw new Error('Use the supervised browser runner');
  const observation = { requests: [] as string[], errors: [] as string[] }; observations.set(page, observation);
  page.on('pageerror', error => observation.errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') observation.errors.push(message.text()); });
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin === baseURL && request.method() === 'GET' && url.pathname === '/derived-value-fixture') {
      await route.fulfill({ contentType: 'text/html', body: '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Generated derived values</title></head><body><div id="root"></div></body></html>' });
    } else if (url.origin === baseURL && request.method() === 'GET' && url.pathname === '/derived-value.js') {
      await route.fulfill({ contentType: 'text/javascript', body: script });
    } else { observation.requests.push(`${request.method()}:${url.origin}${url.pathname}`); await route.abort(); }
  });
  await page.addInitScript(() => {
    let kind: 'success' | 'reject' | 'throw' | 'absent' | 'deferred' = 'success';
    const writes: string[] = [];
    const pending = new Map<number, { resolve: () => void; reject: (error: Error) => void }>();
    const rawError = () => new Error('RAW_CLIPBOARD_ERROR_GENERATED_SENTINEL');
    const clipboard = { writeText(value: string) {
      const index = writes.push(value) - 1;
      if (kind === 'throw') throw rawError();
      if (kind === 'reject') return Promise.reject(rawError());
      if (kind === 'deferred') return new Promise<void>((resolve, reject) => pending.set(index, { resolve, reject }));
      return Promise.resolve();
    } };
    window.clipboardFixture = {
      writes,
      configure(next) { kind = next; Object.defineProperty(navigator, 'clipboard', { configurable: true, value: next === 'absent' ? undefined : clipboard }); },
      settle(index, success) { const operation = pending.get(index); if (!operation) throw new Error('Unknown generated copy request'); pending.delete(index); if (success) operation.resolve(); else operation.reject(rawError()); },
    };
    window.clipboardFixture.configure('success');
  });
  await page.setViewportSize({ width, height: 900 });
  await page.goto(`${baseURL}/derived-value-fixture?mode=${mode}`);
  await page.addStyleTag({ content: styles + `html { font-size: ${scale}% }` });
  await page.addScriptTag({ type: 'module', url: `${baseURL}/derived-value.js` });
  await expect(page.getByRole('heading', { name: 'Generated derived values' })).toBeVisible();
  await page.evaluate(async () => { await document.fonts.load('500 14px Geist'); await document.fonts.load('400 12px Geist'); await document.fonts.ready; });
  await expect(page.locator('.ot-derived-value')).toHaveCount(2);
  expect(await page.evaluate(() => document.fonts.check('500 14px Geist'))).toBe(true);
}
const firstButton = (page: Page) => page.locator('.derived-first').getByRole('button', { name: fixtures.first.copyLabel, exact: true });
const firstStatus = (page: Page) => page.locator('.derived-first').getByRole('status');
async function capture(page: Page, info: TestInfo, name: string, selector?: string) {
  const bytes = selector ? await page.locator(selector).screenshot({ animations: 'disabled' }) : await page.screenshot({ fullPage: true, animations: 'disabled' });
  await writeFile(info.outputPath(name), bytes);
  return { path: name, sha256: createHash('sha256').update(bytes).digest('hex') };
}
async function evidence(page: Page, info: TestInfo, name: string, data: object) {
  await writeFile(info.outputPath(name), JSON.stringify({ ...data, fixture_kind: 'generated-presentation', clipboard: 'controlled navigator.clipboard.writeText adapter; no real OS clipboard claim',
    manual_screen_reader: 'NOT_RUN', requests_rejected: observations.get(page)?.requests, browser_errors: observations.get(page)?.errors }, null, 2));
}
async function geometry(page: Page) {
  return page.locator('.ot-derived-value').evaluateAll(nodes => nodes.map(node => {
    const box = (element: Element) => { const { x, y, width, height } = element.getBoundingClientRect(); return { x, y, width, height }; };
    const display = node.querySelector('.ot-derived-display')!, label = node.querySelector('.ot-derived-label')!, value = node.querySelector('.ot-derived-text')!, helper = node.querySelector('.ot-derived-help')!, button = node.querySelector('button')!;
    const font = (element: Element) => { const style = getComputedStyle(element); return [style.fontSize, style.lineHeight, style.fontWeight, style.color]; };
    return { display: box(display), label: box(label), value: box(value), helper: box(helper), button: box(button), displayStyle: [getComputedStyle(display).borderTopWidth, getComputedStyle(display).borderTopColor, getComputedStyle(display).borderRadius, getComputedStyle(display).backgroundColor],
      labelFont: font(label), valueFont: font(value), helperFont: font(helper), exact: value.textContent, selectable: getComputedStyle(value).userSelect,
      noClip: [...node.querySelectorAll('*')].every(element => element.scrollWidth <= element.clientWidth + 1 && element.scrollHeight <= element.clientHeight + 1) };
  }));
}

for (const mode of fixtures.modes) {
  for (const width of fixtures.widths) for (const scale of [100, 200]) {
    test(`DV01 ${mode} derived values at ${width}px ${scale}% text preserve contents and native geometry`, async ({ page, baseURL }, info) => {
      await open(page, baseURL, mode, width, scale);
      const measured = await geometry(page);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
      expect(await page.locator('form').evaluate(form => [...new FormData(form as HTMLFormElement).entries()])).toEqual([]);
      await expect(page.locator('.ot-derived-value input, .ot-derived-value textarea, .ot-derived-value select, .ot-derived-value [contenteditable], .ot-derived-value [name]')).toHaveCount(0);
      expect(measured.map(row => row.exact)).toEqual([fixtures.first.value, fixtures.long.value]);
      for (const row of measured) {
        expect(row.selectable).toBe('text'); expect(row.noClip).toBe(true);
        expect(row.display.width).toBe(width === 320 ? 288 : 360);
        expect(row.button.width).toBe(width === 320 ? 288 : 300); expect(row.button.height).toBeGreaterThanOrEqual(44);
        expect(row.button.y - row.display.y - row.display.height).toBe(16);
        expect(row.labelFont).toEqual([`${14 * scale / 100}px`, `${20 * scale / 100}px`, '500', 'rgb(24, 34, 55)']);
        expect(row.valueFont).toEqual(row.labelFont);
        expect(row.helperFont).toEqual([`${12 * scale / 100}px`, `${18 * scale / 100}px`, '400', 'rgb(81, 96, 120)']);
        expect(row.displayStyle).toEqual(['1px', 'rgb(98, 111, 132)', '8px', 'rgb(255, 255, 255)']);
      }
      if (scale === 100) {
        expect(measured[0].display.height).toBe(108); expect(measured[0].button.height).toBe(44);
        expect([measured[0].label.x - measured[0].display.x, measured[0].label.y - measured[0].display.y,
          measured[0].value.y - measured[0].display.y, measured[0].helper.y - measured[0].display.y]).toEqual([17, 17, 45, 73]);
      }
      const associations = await page.locator('.ot-derived-copy').evaluateAll(groups => groups.map(group => ({ name: document.getElementById(group.getAttribute('aria-labelledby')!)!.textContent,
        descriptions: group.getAttribute('aria-describedby')!.split(' ').map(id => document.getElementById(id)!.textContent) })));
      expect(associations).toEqual([{ name: fixtures.first.label, descriptions: [fixtures.first.value, fixtures.first.helpText] }, { name: fixtures.long.label, descriptions: [fixtures.long.value, fixtures.long.helpText] }]);
      const selected = await page.locator('.derived-long .ot-derived-text').evaluate(node => { const range = document.createRange(); range.selectNodeContents(node); const selection = window.getSelection()!; selection.removeAllRanges(); selection.addRange(range); const exact = selection.toString(); selection.removeAllRanges(); return exact; });
      expect(selected).toBe(fixtures.long.value);
      expect(await page.evaluate(() => window.clipboardFixture.writes)).toEqual([]);
      const captures = [await capture(page, info, `derived-value-${mode}-${width}-${scale}.png`)];
      if (width === 1280 && scale === 100) captures.push(await capture(page, info, `derived-value-${mode}-native.png`, '.derived-first .ot-derived-display'));
      await evidence(page, info, 'derived-value-geometry.json', { mode, width, scale, measured, associations, captures,
        native_display: ['12:34', '154:443'], native_copy: '166:8646', native_combined_specimen: false });
    });
  }

  test(`DV02 ${mode} named keyboard and pointer copy writes exact current text only after activation`, async ({ page, baseURL }, info) => {
    await open(page, baseURL, mode);
    const exact = '  0007α\n<exact & generated>  ';
    await page.evaluate(value => { window.derivedFixture.replace({ value }); window.clipboardFixture.configure('deferred'); }, exact);
    await expect(page.locator('.derived-first .ot-derived-text')).toHaveText(exact, { useInnerText: false });
    await page.getByRole('button', { name: 'Before values', exact: true }).focus(); await page.keyboard.press('Tab');
    await expect(firstButton(page)).toBeFocused(); await firstButton(page).hover();
    await page.locator('.derived-first .ot-derived-text').click();
    expect(await page.evaluate(() => window.clipboardFixture.writes)).toEqual([]);
    await firstButton(page).focus();
    await page.evaluate(() => {
      const status = document.querySelector('.derived-first [role=status]')!;
      const observation = { mutations: 0, updates: [] as string[], button: document.querySelector('.derived-first button')!, value: document.querySelector('.derived-first .ot-derived-text')! };
      window.derivedObservation = observation;
      new MutationObserver(records => { observation.mutations += records.length; observation.updates.push(status.textContent!); }).observe(status, { subtree: true, childList: true, characterData: true });
    });
    await page.keyboard.press('Enter');
    await expect(firstButton(page)).toHaveAttribute('aria-busy', 'true'); await expect(firstStatus(page)).toBeEmpty();
    expect(await page.evaluate(() => window.clipboardFixture.writes)).toEqual([exact]);
    await page.evaluate(() => window.clipboardFixture.settle(0, true));
    await expect(firstStatus(page)).toHaveText(fixtures.first.copyFeedback.success); await expect(firstButton(page)).toBeFocused();
    expect(await page.evaluate(() => window.derivedObservation.updates)).toEqual(['Copied']);
    const count = await page.evaluate(() => window.derivedObservation.mutations);
    for (let index = 1; index <= 3; index++) { await page.evaluate(() => window.derivedFixture.rerender()); await expect(page.locator('main')).toHaveAttribute('data-renders', String(index)); }
    expect(await page.evaluate(() => window.derivedObservation.mutations)).toBe(count);
    expect(await page.evaluate(() => window.derivedObservation.button === document.querySelector('.derived-first button') && window.derivedObservation.value === document.querySelector('.derived-first .ot-derived-text'))).toBe(true);
    await page.keyboard.press('Tab'); const second = page.locator('.derived-long').getByRole('button'); await expect(second).toBeFocused();
    await page.keyboard.press('Space'); await page.evaluate(() => window.clipboardFixture.settle(1, true));
    await expect(page.locator('.derived-long').getByRole('status')).toHaveText('Copied');
    await page.keyboard.press('Tab'); await expect(page.getByRole('button', { name: 'After values', exact: true })).toBeFocused();
    await firstButton(page).click(); await page.evaluate(() => window.clipboardFixture.settle(2, true)); await expect(firstStatus(page)).toHaveText('Copied');
    expect(await page.evaluate(() => window.clipboardFixture.writes)).toEqual([exact, fixtures.long.value, exact]);
    expect(await page.locator('.ot-derived-value [role=status]').count()).toBe(2); await expect(page.locator('.ot-derived-value [aria-live], .ot-derived-value [role=alert]')).toHaveCount(0);
    const image = await capture(page, info, `derived-value-${mode}-keyboard-copied.png`);
    await evidence(page, info, 'derived-value-copy.json', { mode, calls: await page.evaluate(() => window.clipboardFixture.writes), initial_status_updates: ['Copied'], rerender_mutations: 0, image });
  });

  test(`DV03 ${mode} rejected unavailable and throwing clipboards keep the exact value and truthful feedback`, async ({ page, baseURL }, info) => {
    await open(page, baseURL, mode);
    for (const kind of ['reject', 'absent', 'throw'] as const) {
      await page.evaluate(kind => { window.clipboardFixture.configure(kind); window.derivedFixture.replace({ helpText: `Generated ${kind} clipboard example` }); }, kind);
      await expect(firstStatus(page)).toBeEmpty();
      await firstButton(page).click();
      await expect(firstStatus(page)).toHaveText(fixtures.first.copyFeedback.failure); await expect(firstButton(page)).toBeFocused();
      expect(await page.locator('.derived-first .ot-derived-text').textContent()).toBe(fixtures.first.value);
      await expect(page.locator('body')).not.toContainText('RAW_CLIPBOARD_ERROR_GENERATED_SENTINEL');
      await expect(firstStatus(page)).not.toContainText('Copied'); await expect(firstStatus(page)).not.toContainText('denied');
    }
    expect(await page.evaluate(() => window.clipboardFixture.writes)).toEqual([fixtures.first.value, fixtures.first.value]);
    const image = await capture(page, info, `derived-value-${mode}-clipboard-failed.png`);
    await evidence(page, info, 'derived-value-failure.json', { mode, cases: ['rejected Promise', 'API absent', 'synchronous exception'], safe_feedback: fixtures.first.copyFeedback.failure, feedback_origin: 'caller-supplied generated fixture, not native permission-denial copy', image });
  });

  test(`DV04 ${mode} pending copy deduplicates and obsolete settlements cannot replace current feedback`, async ({ page, baseURL }, info) => {
    await open(page, baseURL, mode); await page.evaluate(() => window.clipboardFixture.configure('deferred'));
    await firstButton(page).click();
    for (let i = 0; i < 5; i++) await firstButton(page).dispatchEvent('click');
    expect(await page.evaluate(() => window.clipboardFixture.writes)).toHaveLength(1); await expect(firstStatus(page)).toBeEmpty();
    await page.evaluate(() => window.derivedFixture.replace({ value: 'generated-new-0008' }));
    await expect(firstButton(page)).not.toHaveAttribute('aria-busy', 'true'); await firstButton(page).click();
    await page.evaluate(() => window.clipboardFixture.settle(1, true)); await expect(firstStatus(page)).toHaveText('Copied');
    await page.evaluate(() => window.clipboardFixture.settle(0, false)); await expect(firstStatus(page)).toHaveText('Copied');
    await firstButton(page).click();
    await page.evaluate(() => window.derivedFixture.replace({ value: 'generated-current-0009' }));
    await expect(firstButton(page)).not.toHaveAttribute('aria-busy', 'true'); await firstButton(page).click();
    await page.evaluate(() => window.clipboardFixture.settle(3, false)); await expect(firstStatus(page)).toHaveText(fixtures.first.copyFeedback.failure);
    await page.evaluate(() => window.clipboardFixture.settle(2, true)); await expect(firstStatus(page)).toHaveText(fixtures.first.copyFeedback.failure);
    await firstButton(page).click(); await page.evaluate(() => window.derivedFixture.show(false)); await expect(page.locator('.derived-first')).toHaveCount(0);
    await page.evaluate(() => window.clipboardFixture.settle(4, true)); await expect(page.locator('body')).not.toContainText('generated-current-0009');
    await page.evaluate(() => { window.derivedFixture.replace({ value: 'generated-remounted-0010' }); window.derivedFixture.show(true); });
    await expect(firstStatus(page)).toBeEmpty(); await expect(firstButton(page)).not.toHaveAttribute('aria-busy', 'true');
    await firstButton(page).click(); await page.evaluate(() => window.clipboardFixture.settle(5, true)); await expect(firstStatus(page)).toHaveText('Copied');
    // Contextual replacement with identical value also invalidates old status; caller removal controls visibility.
    await page.evaluate(() => window.derivedFixture.replace({ label: 'Current generated reference' })); await expect(firstStatus(page)).toBeEmpty();
    const calls = await page.evaluate(() => window.clipboardFixture.writes);
    expect(calls).toEqual([fixtures.first.value, 'generated-new-0008', 'generated-new-0008', 'generated-current-0009', 'generated-current-0009', 'generated-remounted-0010']);
    const image = await capture(page, info, `derived-value-${mode}-race-current.png`);
    await evidence(page, info, 'derived-value-races.json', { mode, calls, duplicate_pending_writes: 0, stale_failure_ignored: true, stale_success_ignored: true, unmounted_success_ignored: true,
      limitation: 'A clipboard write already initiated cannot be recalled; only obsolete feedback is suppressed.', image });
  });

  for (const treatment of ['forced-colors', 'reduced-motion'] as const) {
    test(`DV05 ${mode} ${treatment} preserves readable values and keyboard focus`, async ({ page, baseURL }, info) => {
      await open(page, baseURL, mode);
      await page.emulateMedia(treatment === 'forced-colors' ? { forcedColors: 'active' } : { reducedMotion: 'reduce' });
      await page.getByRole('button', { name: 'Before values', exact: true }).focus(); await page.keyboard.press('Tab'); await expect(firstButton(page)).toBeFocused();
      await expect(firstButton(page)).toHaveCSS('outline-width', '2px'); await expect(firstButton(page)).toHaveCSS('outline-offset', '2px');
      expect(await page.evaluate(() => document.getAnimations().length)).toBe(0);
      const measured = await geometry(page); expect(measured.every(row => row.noClip && row.selectable === 'text')).toBe(true);
      if (treatment === 'forced-colors') {
        const highlight = await page.evaluate(() => { const node = document.createElement('span'); node.style.cssText = 'color:Highlight;forced-color-adjust:none'; document.body.append(node); const color = getComputedStyle(node).color; node.remove(); return color; });
        await expect(firstButton(page)).toHaveCSS('outline-color', highlight); await expect(firstButton(page)).toHaveCSS('border-style', 'solid');
      }
      const image = await capture(page, info, `derived-value-${mode}-${treatment}.png`);
      await evidence(page, info, 'derived-value-treatment.json', { mode, treatment, measured, image });
    });
  }
}
