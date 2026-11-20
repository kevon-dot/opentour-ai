import { expect, type Page, type TestInfo } from '@playwright/test';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { buildInlineFixture } from './build.ts';

export function buildEvidenceFixture(name: string) {
  return buildInlineFixture(join(process.cwd(), 'apps/web/tests/fixtures/presentation', name + '.tsx'), 'iife');
}

export async function mountEvidenceFixture(page: Page, fixture: Awaited<ReturnType<typeof buildEvidenceFixture>>,
  width: number, scale: number) {
  await page.setViewportSize({ width, height: 900 });
  await page.setContent('<!doctype html><html lang="en"><title>Evidence fixture</title><body><div id="root"></div></body></html>');
  const inset = width === 320 ? 16 : width === 768 ? 24 : 32;
  await page.addStyleTag({ content: fixture.css + `html {font-size:${scale}%} body {margin:0;background:var(--ot-surface-canvas)} main {padding:${inset}px}` });
  await page.evaluate(fixture.script);
}

export async function captureEvidence(page: Page, info: TestInfo, stem: string) {
  await page.evaluate(() => document.fonts.ready);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
  await page.screenshot({ path: info.outputPath(stem + '.png'), fullPage: true, animations: 'disabled' });
  await writeFile(info.outputPath(stem + '.json'), JSON.stringify({ aria: await page.getByRole('main').ariaSnapshot() }));
}
