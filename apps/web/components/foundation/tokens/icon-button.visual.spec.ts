import { expect, test } from '@playwright/test';
import { join } from 'node:path';
import { build } from 'vite';

test.use({ hasTouch: true });

let script: string;
let styles: string;
test.beforeAll(async () => {
  const root = join(process.cwd(), 'apps/web');
  const result = await build({
    root, configFile: false, envFile: false, publicDir: false, logLevel: 'error',
    oxc: { jsx: { runtime: 'automatic' } },
    build: { write: false, lib: { entry: join(root, 'tests/fixtures/presentation/icon-button.tsx'), formats: ['es'] } },
    define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  });
  const bundle = Array.isArray(result) ? result[0] : result;
  if (!('output' in bundle)) throw new Error('Missing icon gallery');
  const js = bundle.output.find(item => item.type === 'chunk');
  const css = bundle.output.find(item => item.type === 'asset' && item.fileName.endsWith('.css'));
  if (!js || js.type !== 'chunk' || !css || css.type !== 'asset') throw new Error('Missing icon gallery assets');
  script = js.code;
  styles = String(css.source);
});

for (const width of [320, 768, 1280, 1440]) for (const scale of [100, 200]) {
  test(`icon button at ${width}px and ${scale}% text`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route('http://icon.test/', route => route.fulfill({ contentType: 'text/html',
      body: '<!doctype html><html lang="en"><main></main></html>' }));
    await page.route('http://icon.test/gallery.js', route => route.fulfill({ contentType: 'text/javascript', body: script }));
    await page.goto('http://icon.test/');
    await page.addStyleTag({ content: styles + `
      html { font-size: ${scale}%; }
      body { margin: 0; }
    ` });
    await page.addScriptTag({ type: 'module', url: 'http://icon.test/gallery.js' });
    const button = (state: string) => page.getByRole('button', { name: 'Close ' + state + ' example', exact: true });
    const count = page.getByRole('status', { name: 'Activation count' });
    await expect(button('default')).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true);
    const initial = await button('focus').boundingBox();
    for (const state of ['default', 'hover', 'focus']) {
      await page.keyboard.press('Tab');
      await expect(button(state)).toBeFocused();
    }
    await button('hover').hover();
    await expect(button('hover')).toHaveCSS('background-color', 'rgb(237, 243, 255)');
    await expect(button('focus')).toHaveCSS('outline', '2px solid rgb(36, 91, 238)');
    await expect(button('focus')).toHaveCSS('outline-offset', '2px');
    expect(await button('focus').boundingBox()).toEqual(initial);
    await expect(button('disabled')).toBeDisabled();
    await expect(button('disabled')).toHaveCSS('color', 'rgb(98, 111, 132)');
    await expect(button('loading')).toHaveAttribute('aria-busy', 'true');
    await expect(button('loading')).toHaveText('…');
    await expect(button('loading')).toHaveCSS('color', 'rgb(81, 96, 120)');
    for (const state of ['default', 'hover', 'focus', 'disabled', 'loading']) {
      const control = button(state);
      const box = (await control.boundingBox())!;
      expect([box.width, box.height]).toEqual([44, 44]);
      await expect(control).toHaveCSS('border', '1px solid rgb(98, 111, 132)');
      await expect(control).toHaveCSS('border-radius', '8px');
      if (state !== 'hover') await expect(control).toHaveCSS('background-color', 'rgb(255, 255, 255)');
      if (state !== 'loading') {
        const art = control.locator('[aria-hidden="true"]');
        const slot = (await art.boundingBox())!;
        expect([slot.x-box.x, slot.y-box.y, slot.width, slot.height]).toEqual([12, 12, 20, 20]);
        expect(await art.evaluate(el => getComputedStyle(el).maskImage)).toContain('svg');
      }
      await page.screenshot({ path: info.outputPath(`icon-button-${width}-${scale}-${state}.png`),
        clip: { x: box.x - 4, y: box.y - 4, width: 52, height: 52 } });
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
    await page.keyboard.press('Enter'); await expect(count).toHaveText('1');
    await page.keyboard.press('Space'); await expect(count).toHaveText('2');
    await page.keyboard.press('Tab'); await expect(button('loading')).toBeFocused();
    await page.keyboard.press('Space'); await expect(count).toHaveText('2');
    await button('disabled').click({ force: true }); await expect(count).toHaveText('2');
    await button('default').click(); await expect(count).toHaveText('3');
    await expect(button('default')).toBeFocused();
    await expect(button('default')).toHaveAttribute('aria-busy', 'true');
    await button('default').click({ force: true }); await page.keyboard.press('Enter');
    await expect(count).toHaveText('3');
    await page.emulateMedia({ forcedColors: 'active', reducedMotion: 'reduce' });
    await page.keyboard.press('Tab');
    await expect(button('hover')).toBeFocused();
    await expect(button('hover')).toHaveCSS('outline-width', '2px');
    const systemColors = await page.evaluate(() => {
      const swatch = document.createElement('span');
      swatch.style.forcedColorAdjust = 'none';
      document.body.append(swatch);
      const colors = ['Highlight', 'ButtonText', 'GrayText'].map(color => {
        swatch.style.color = color;
        return getComputedStyle(swatch).color;
      });
      swatch.remove();
      return colors;
    });
    await expect(button('hover')).toHaveCSS('outline-color', systemColors[0]);
    await expect(button('hover')).toHaveCSS('border-color', systemColors[1]);
    await expect(button('disabled').locator('span')).toHaveCSS('background-color', systemColors[2]);
    await expect(button('loading').locator('span')).toHaveCSS('animation-name', 'none');
  });
}
