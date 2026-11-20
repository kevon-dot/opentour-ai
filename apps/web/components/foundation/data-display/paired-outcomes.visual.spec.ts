import { expect, test, type Page } from '@playwright/test';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { buildInlineFixture } from '../../../tests/fixtures/presentation/build.ts';
import type { PairedFixtureName } from '../../../tests/fixtures/presentation/paired-outcomes';

let gallery: Awaited<ReturnType<typeof buildInlineFixture>>;
test.beforeAll(async () => {
  gallery = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/paired-outcomes.tsx'), 'iife');
});

const shellCss = `
  body { margin: 0; background: var(--ot-surface-canvas); color: var(--ot-text-primary); font-family: Geist,sans-serif; }
  header, main { display: grid; gap: var(--ot-space-16); }
  header { padding: var(--ot-space-16); gap: var(--ot-space-8); background: white; border: 1px solid var(--ot-border-default); }
  p, h1 { margin: 0; }
  header p { font: 500 var(--ot-type-label-size)/var(--ot-type-label-line) Geist,sans-serif; }
  header small { font: var(--ot-type-small-size)/var(--ot-type-small-line) Geist,sans-serif; color: var(--ot-text-secondary); }
  main { padding: var(--ot-space-32); }
  main > p { font: var(--ot-type-body-size)/var(--ot-type-body-line) Geist,sans-serif; color: var(--ot-text-secondary); }
  h1 { font: 600 var(--ot-type-heading-size)/var(--ot-type-heading-line) Geist,sans-serif; }
  @media (max-width: 1199px) { main { padding: var(--ot-space-24); } }
  @media (max-width: 599px) { main { padding: var(--ot-space-16); } }
`;
const labels = ['Both correct', 'Fixed', 'Regressed', 'Both incorrect', 'Excluded', 'Unknown',
  'Complete pairs', 'Missing candidate results', 'Expected pairs'];
const unknown = 'Unknown · Count not supplied';
const expectedRows = {
  complete: ['130', '40', '10', '20', '0', '0', '200', '0', '200'],
  partial: [unknown, unknown, unknown, unknown, unknown, unknown, '180', '20', '200'],
  mixed: ['4', '0', unknown, 'Missing · Result unavailable', '1', '1', '6', '2', '10'],
};
const ratios = { complete: [.65, .2, .05, .1], partial: [], mixed: [4 / 6] };

async function measure(page: Page) {
  return page.evaluate(() => {
    const marks = Array.from(document.querySelectorAll('.ot-paired-mark'), mark => {
      const key = mark.closest('li')!.dataset.series!;
      const swatch = document.querySelector(`tr[data-series="${key}"] th .ot-table-cell-value`)!;
      return {
        ratio: mark.getBoundingClientRect().width / mark.parentElement!.getBoundingClientRect().width,
        color: getComputedStyle(mark).backgroundColor,
        tableColor: getComputedStyle(swatch, '::before').backgroundColor,
      };
    });
    const clipped = Array.from(document.querySelectorAll('.ot-paired :is(li, th, td, figcaption, caption, .ot-table-cell-value)'))
      .filter(el => el.clientWidth > 0 && (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1))
      .map(el => el.textContent);
    return { marks, clipped, overflow: document.documentElement.scrollWidth - innerWidth,
      labelFontSize: getComputedStyle(document.querySelector('.ot-paired li')!).fontSize };
  });
}

for (const width of [320, 768, 1280, 1440]) {
  for (const scale of [100, 200]) {
    test(`paired outcomes at ${width}px and ${scale}% text`, async ({ page }, info) => {
      await page.setViewportSize({ width, height: 900 });
      await page.setContent('<!doctype html><html lang="en"><meta charset="utf-8"><title>Paired outcomes fixture</title><div id="root"></div></html>');
      await page.addStyleTag({ content: gallery.css + shellCss + `html { font-size: ${scale}% }` });
      await page.addScriptTag({ content: gallery.script });
      for (const state of ['complete', 'partial', 'mixed'] as const) {
        await page.evaluate((name: PairedFixtureName) => window.showPairedFixture(name), state);
        const table = page.getByRole('table', { name: 'Paired outcomes · Exact values' });
        const figure = page.getByRole('figure', { name: 'Paired outcomes' });
        const values = expectedRows[state];
        await expect(figure.locator('figcaption')).toHaveText(`Learning · ${values[6]} complete pairs`);
        await expect(figure.getByRole('listitem')).toHaveText(labels.slice(0, 4).map((label, i) => `${label} · ${values[i]}`));
        await expect(table.getByRole('columnheader')).toHaveText(['Measure', 'Exact value']);
        await expect(table.getByRole('rowheader')).toHaveText(labels);
        await expect(table.getByRole('cell')).toHaveText(values);
        await expect(table.locator('th[scope=col]')).toHaveCount(2);
        await expect(table.locator('th[scope=row]')).toHaveCount(9);
        await page.evaluate(() => document.fonts.ready);
        const geometry = await measure(page);
        expect(geometry.clipped).toEqual([]);
        expect(geometry.overflow).toBe(0);
        expect(geometry.labelFontSize).toBe(scale === 100 ? '12px' : '24px');
        expect(geometry.marks).toHaveLength(ratios[state].length);
        for (const [i, mark] of geometry.marks.entries()) {
          expect(mark.ratio).toBeCloseTo(ratios[state][i], 3);
          expect(mark.color).toBe(mark.tableColor);
          expect(mark.color).toBe(state === 'complete' && i === 2 ? 'rgb(140, 91, 10)' : 'rgb(36, 91, 238)');
        }
        // Exact values are already visible. Reading this static chart requires no focus or hover.
        await expect(page.locator('.ot-paired :is(button, a, input, [tabindex])')).toHaveCount(0);
        const name = `paired-${state}-${width}-${scale}`;
        await page.screenshot({ path: info.outputPath(name + '.png'), fullPage: true, animations: 'disabled' });
        await writeFile(info.outputPath(name + '.json'), JSON.stringify({ ...geometry, aria: await table.ariaSnapshot() }));
      }
    });
  }
}
