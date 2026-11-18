import { expect, test } from '@playwright/test';
import { join } from 'node:path';
import { buildInlineFixture } from '../../../tests/fixtures/presentation/build.ts';

test.use({ hasTouch: true });

let script: string;
let styles: string;
test.beforeAll(async () => {
  const output = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/difference-review.tsx'), 'esm');
  script = output.script; styles = output.css;
});

for (const width of [320, 768, 1280, 1440]) for (const scale of [100, 200]) {
  test(`difference review at ${width}px and ${scale}% text`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route('http://comparison.test/', route => route.fulfill({ contentType: 'text/html',
      body: '<!doctype html><html lang="en"><main></main></html>' }));
    await page.route('http://comparison.test/gallery.js', route => route.fulfill({ contentType: 'text/javascript', body: script }));
    await page.goto('http://comparison.test/');
    await page.addStyleTag({ content: styles + `html{font-size:${scale}%}body{margin:0}
      main{box-sizing:border-box;padding:32px;display:grid;gap:24px;justify-items:start}` });
    await page.addScriptTag({ type: 'module', url: 'http://comparison.test/gallery.js' });
    const card = page.getByRole('region', { name: 'Maximum attempts', exact: true });
    const adverse = page.getByRole('region', { name: 'Retry delay for this generated field' });
    await expect(card).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    await expect(card.getByRole('group', { name: 'Your draft · revision 7', exact: true })).toHaveText('Your draft · revision 7: 3');
    await expect(card.getByRole('group', { name: 'Current settings · revision 8', exact: true })).toHaveText('Current settings · revision 8: 5');
    const rawDraft = adverse.getByRole('group', { name: 'Your draft · revision 007' });
    expect(await rawDraft.locator('span').last().textContent()).toBe('  0003?  \n' + 'unparsed'.repeat(18));
    await expect(rawDraft).toHaveCSS('white-space', 'pre-wrap');
    const keep = card.getByRole('button', { name: 'Keep current value' });
    const draft = card.getByRole('button', { name: 'Use my draft' });
    const bounds = (await card.boundingBox())!;
    const first = (await keep.boundingBox())!;
    const second = (await draft.boundingBox())!;
    expect(bounds.width).toBe(width === 320 ? 256 : 688);
    if (scale === 100) {
      expect(bounds.height).toBe(width === 320 ? 214 : 158);
      expect([first.x-bounds.x, first.y-bounds.y, first.width, first.height]).toEqual([13, 101, width === 320 ? 232 : 280, 44]);
      expect([second.x-first.x, second.y-first.y]).toEqual(width === 320 ? [0, 56] : [292, 0]);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);
    for (const [name, region] of [['reference', card], ['raw', adverse]] as const) {
      const fits = await region.evaluate(node => [...node.querySelectorAll('*')].every(el => el.scrollWidth <= el.clientWidth + 1 || getComputedStyle(el).display === 'inline'));
      expect(fits).toBe(true);
      await region.screenshot({path:info.outputPath(`difference-${width}-${scale}-${name}.png`)});
    }
    const output = page.getByRole('status', { name: 'Requested choices' });
    await page.keyboard.press('Tab'); await expect(keep).toBeFocused();
    await page.keyboard.press('Enter'); await expect(output).toHaveText('maximum-attempts:current');
    await page.keyboard.press('Tab'); await expect(draft).toBeFocused();
    await page.keyboard.press('Space'); await expect(output).toHaveText('maximum-attempts:current, maximum-attempts:draft');
    await adverse.getByRole('button', { name: 'Use my draft' }).click();
    await expect(output).toHaveText('maximum-attempts:current, maximum-attempts:draft, retry-delay:draft');
    await adverse.getByRole('button', { name: 'Keep current value' }).click();
    await expect(output).toHaveText('maximum-attempts:current, maximum-attempts:draft, retry-delay:draft, retry-delay:current');
    expect(await rawDraft.locator('span').last().textContent()).toContain('0003?');
    await page.emulateMedia({ forcedColors: 'active' });
    await page.keyboard.press('Shift+Tab'); await expect(draft).toBeFocused();
    const highlight = await page.evaluate(() => {
      const swatch = document.createElement('span');
      swatch.style.cssText = 'forced-color-adjust:none;color:Highlight';
      document.body.append(swatch);
      const color = getComputedStyle(swatch).color;
      swatch.remove();
      return color;
    });
    await expect(draft).toHaveCSS('outline-color', highlight);
    await expect(draft).toHaveCSS('outline-width', '2px');
    await expect(card).toHaveCSS('border-style', 'solid');
  });
}
