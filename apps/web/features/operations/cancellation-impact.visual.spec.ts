import { expect, test } from '@playwright/test';
import { join } from 'node:path';
import { buildInlineFixture } from '../../tests/fixtures/presentation/build.ts';

let script: string;
let styles: string;
test.beforeAll(async () => {
  const output = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/cancellation-impact.tsx'), 'esm');
  script = output.script; styles = output.css;
});

for (const width of [320, 768, 1280, 1440]) for (const scale of [100, 200]) {
  test(`cancellation tables at ${width}px and ${scale}% text`, async ({ page }, info) => {
    const pad = width === 320 ? 16 : width === 768 ? 24 : 32;
    await page.setViewportSize({ width, height: 1050 });
    await page.route('http://impact.test/**', route => {
      if (route.request().url().endsWith('/gallery.js')) return route.fulfill({ contentType: 'text/javascript', body: script });
      const adverse = route.request().url().endsWith('/adverse');
      return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html lang="en"><title>Cancellation observations</title><main' +
        (adverse ? ' data-adverse="true"' : '') + '></main></html>' });
    });
    const gallery = page.locator('.ot-cancellation-impact');
    const regions = page.getByRole('region');
    async function open(path: string) {
      await page.goto('http://impact.test/' + path);
      await page.addStyleTag({ content: styles + `html{font-size:${scale}%}body{margin:0;padding:${pad}px;background:var(--ot-surface-canvas)}` });
      await page.addScriptTag({ type: 'module', url: 'http://impact.test/gallery.js' });
      await expect(gallery).toBeVisible();
      await page.evaluate(() => document.fonts.ready);
    }
    await open('');
    await expect(page.getByRole('table')).toHaveCount(2);
    await expect(page.getByRole('columnheader')).toHaveCount(4);
    await expect(page.getByRole('rowheader')).toHaveCount(7);
    for (const value of ['USD 7.40 accrued · not final', 'res-42 · USD 25.00 maximum', 'Nov 12, 09:42 UTC',
      '240 seconds at observation', 'Checkpoint cp-7 · retention policy', 'Retained under policy',
      'Outside OpenTour control · cannot be recalled']) {
      await expect(page.getByRole('cell', { name: value, exact: true })).toBeAttached();
    }
    expect(await gallery.evaluate(node => node.getBoundingClientRect().width)).toBe(width - 2 * pad);
    const blocks = gallery.locator('.ot-impact-block');
    if (scale === 100) {
      expect((await blocks.first().boundingBox())?.height).toBe(294);
      expect((await blocks.last().boundingBox())?.height).toBe(162);
    }
    await gallery.screenshot({ path: info.outputPath(`impact-${width}-${scale}.png`) });
    await page.keyboard.press('Tab');
    await expect(regions.first()).toBeFocused();
    if (width === 320) {
      await expect(regions.first()).toHaveAccessibleName('Cancellation impact — scroll horizontally');
      await page.keyboard.press('ArrowRight');
      await expect.poll(() => regions.first().evaluate(node => node.scrollLeft)).toBeGreaterThan(0);
    }
    await page.keyboard.press('Tab');
    await expect(regions.last()).toBeFocused();
    await open('adverse');
    await expect(page.getByRole('cell', { name: 'Unknown', exact: true })).toHaveCount(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.emulateMedia({ forcedColors: 'active' });
    await page.keyboard.press('Tab');
    await expect(regions.first()).toBeFocused();
    await expect(regions.first()).toHaveCSS('outline-style', 'solid');
    await expect(page.getByRole('cell', { name: 'Unknown', exact: true })).toHaveCSS('border-top-style', 'solid');
  });
}
