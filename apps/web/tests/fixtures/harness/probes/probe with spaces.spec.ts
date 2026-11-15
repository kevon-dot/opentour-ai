import { test, expect } from '@playwright/test';
import { writeFileSync } from 'node:fs';
import { guard } from '../fixtures.ts';
const probe = process.env.OPENTOUR_BROWSER_PROBE;
test('actual one-browser probe', async ({ page, context }) => {
  if (probe === 'skip') test.skip(true, 'Intentional negative subprocess probe.');
  if (probe === 'expected_failure') test.fail(true, 'Expected failures cannot become acceptance.');
  const violations = await guard(context);
  await page.goto('/__harness/ready');
  await expect(page.locator('body')).toContainText('simulated-browser-harness');
  if (process.env.OPENTOUR_BROWSER_READY_FILE) writeFileSync(process.env.OPENTOUR_BROWSER_READY_FILE, JSON.stringify({ origin: process.env.OPENTOUR_FIXTURE_ORIGIN }));
  if (probe === 'hold' || probe === 'timeout' || probe === 'interrupt') { test.setTimeout(30_000); await new Promise(() => {}); }
  if (probe === 'network') await page.evaluate(async () => { try { await fetch('https://forbidden.invalid/sentinel'); } catch { /* Guard still records the attempt. */ } });
  expect(violations).toEqual([]);
  expect(probe).not.toBe('fail');
  expect(probe).not.toBe('expected_failure');
});
