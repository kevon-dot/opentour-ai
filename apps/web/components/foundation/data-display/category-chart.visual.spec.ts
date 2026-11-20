import { expect, test } from '@playwright/test';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { buildInlineFixture } from '../../../tests/fixtures/presentation/build.ts';
import type { CategoryFixtureName } from '../../../tests/fixtures/presentation/category-chart';

let gallery: Awaited<ReturnType<typeof buildInlineFixture>>;
test.beforeAll(async () => {
  gallery = await buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation/category-chart.tsx'), 'iife');
});

const scenarios = {
  overflow: {
    chart: ['A 50', 'B 30', 'Other 20'], measures: ['A', 'B', 'Other', 'Other includes'], values: ['50', '30', '20', 'C 10; D 7; E 3'],
    full: ['A', 'B', 'C', 'D', 'E'], exact: ['50', '30', '10', '7', '3'], percentages: [50, 30, 20], cases: 100,
  },
  tied: {
    chart: ['Zulu 4', 'Alpha 4', 'No matching cases 0'], measures: ['Zulu', 'Alpha', 'No matching cases'], values: ['4', '4', '0'],
    full: [], exact: [], percentages: [50, 50], cases: 8,
  },
  incomplete: {
    chart: ['Lengthy category label across narrow widths 3', 'Known zero 0', 'Other Unknown · Aggregate unavailable'],
    measures: ['Lengthy category label across narrow widths', 'Known zero', 'Other', 'Other includes'],
    values: ['3', '0', 'Unknown · Aggregate unavailable', 'Pending count Unavailable · Source unavailable; Unknown count Unknown · Count not supplied'],
    full: ['Lengthy category label across narrow widths', 'Known zero', 'Pending count', 'Unknown count'],
    exact: ['3', '0', 'Unavailable · Source unavailable', 'Unknown · Count not supplied'], percentages: [30], cases: 10,
  },
};

for (const width of [320, 768, 1280, 1440]) {
  for (const scale of [100, 200]) {
    test(`category chart at ${width}px and ${scale}% text`, async ({ page }, info) => {
      await page.setViewportSize({ width, height: 900 });
      await page.setContent('<!doctype html><html lang="en"><title>Cases by category</title><div id="root"></div></html>');
      const inset = width === 320 ? 16 : width === 768 ? 24 : 32;
      await page.addStyleTag({ content: gallery.css + `html {font-size: ${scale}%} body {margin: 0; background: var(--ot-surface-canvas)} main {padding: ${inset}px}` });
      await page.addScriptTag({ content: gallery.script });
      for (const name of Object.keys(scenarios) as CategoryFixtureName[]) {
        const scenario = scenarios[name];
        await page.evaluate(name => window.showCategoryFixture(name), name);
        const figure = page.getByRole('figure', { name: 'Cases by category' });
        const table = page.getByRole('table', { name: 'Accessible data table · cases' });
        await expect(figure.locator('figcaption')).toHaveText('Learning · ' + scenario.cases + ' cases');
        await expect(figure.getByRole('listitem')).toHaveText(scenario.chart);
        await expect(table.getByRole('columnheader')).toHaveText(['Measure', 'Exact value']);
        await expect(table.getByRole('rowheader')).toHaveText(scenario.measures);
        await expect(table.getByRole('cell')).toHaveText(scenario.values);
        await page.evaluate(() => document.fonts.ready);
        const marks = await figure.locator('.ot-category-mark').evaluateAll(nodes => nodes.map(node => {
          const track = node.parentElement!.getBoundingClientRect();
          return { width: node.getBoundingClientRect().width, track: track.width, height: node.getBoundingClientRect().height,
            color: getComputedStyle(node).backgroundColor };
        }));
        expect(marks).toHaveLength(scenario.percentages.length);
        for (const [i, mark] of marks.entries()) {
          expect(mark.track).toBe(Math.min(920, width - 2 * inset) - 68);
          expect(mark.width).toBeCloseTo(mark.track * scenario.percentages[i] / 100, 1);
          expect(mark.height).toBe(16);
          expect(mark.color).toBe('rgb(36, 91, 238)');
        }
        const geometry = await page.evaluate(() => ({
          overflow: document.documentElement.scrollWidth - innerWidth,
          font: getComputedStyle(document.querySelector('li')!).fontSize,
          clipped: [...document.querySelectorAll('li, figcaption, th, td, .ot-table-cell-value')].filter(el =>
            el.clientWidth && (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1)).map(el => el.textContent),
        }));
        expect(geometry).toEqual({ overflow: 0, font: scale === 200 ? '24px' : '12px', clipped: [] });
        const capture = async (state: string) => {
          const path = `category-${name}-${width}-${scale}-${state}`;
          await page.screenshot({ path: info.outputPath(path + '.png'), fullPage: true, animations: 'disabled' });
          await writeFile(info.outputPath(path + '.json'), JSON.stringify({ marks, geometry, aria: await page.getByRole('table').ariaSnapshot() }));
        };
        await capture('summary');
        const button = page.getByRole('button', { name: 'View all categories' });
        if (!scenario.full.length) { await expect(button).toHaveCount(0); continue; }
        const region = page.getByRole('region', { name: width === 320 ? 'Accessible data table — scroll horizontally' : 'Accessible data table', exact: true });
        await page.keyboard.press('Tab');
        await expect(region).toBeFocused();
        if (width === 320) {
          await page.keyboard.press('ArrowRight');
          await expect.poll(() => region.evaluate(el => el.scrollLeft)).toBeGreaterThan(0);
          await region.evaluate(el => { el.scrollLeft = 0; });
        }
        await page.keyboard.press('Tab');
        await expect(button).toBeFocused();
        await page.keyboard.press('Enter');
        const all = page.getByRole('table', { name: 'All categories · cases' });
        const close = page.getByRole('button', { name: 'Show category summary' });
        await expect(close).toBeFocused();
        await expect(close).toHaveAttribute('aria-expanded', 'true');
        await expect(all.getByRole('rowheader')).toHaveText(scenario.full);
        await expect(all.getByRole('cell')).toHaveText(scenario.exact);
        await expect(figure.getByRole('listitem')).toHaveText(scenario.chart);
        await capture('expanded');
        await page.keyboard.press('Space');
        await expect(button).toBeFocused();
        await button.click();
        await expect(all.getByRole('cell')).toHaveText(scenario.exact);
        await close.click();
        await expect(table.getByRole('cell')).toHaveText(scenario.values);
      }
    });
  }
}
