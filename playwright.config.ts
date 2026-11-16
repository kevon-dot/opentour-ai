import { defineConfig } from '@playwright/test';
import { resolve } from 'node:path';

const root = process.env.OPENTOUR_REPOSITORY_ROOT;
const origin = process.env.OPENTOUR_FIXTURE_ORIGIN;
const output = process.env.OPENTOUR_BROWSER_OUTPUT;
if (!root || !origin || !output) throw new Error('Use the owned browser runner.');
const probe = process.env.OPENTOUR_BROWSER_PROBE;
const target = process.env.OPENTOUR_BROWSER_TARGET;
const profile = process.env.OPENTOUR_BROWSER_PROFILE;
const foundation = profile === 'presentation-foundation-v1';
if (foundation ? target !== 'apps/web/components/foundation/tokens/visual.spec.ts' || !!probe :
  profile !== 'simulated-browser-harness' || (!probe && target !== 'apps/web/tests/browser-harness.browser.spec.ts')) {
  throw new Error('Unknown owned browser target/profile pair.');
}
export default defineConfig({
  testDir: root,
  testMatch: probe ? 'apps/web/tests/fixtures/harness/probes/**/*.spec.ts' : [target!],
  testIgnore: probe ? [] : ['**/fixtures/harness/probes/**'],
  outputDir: output,
  fullyParallel: false, workers: 1, retries: 0, repeatEach: 1, forbidOnly: true,
  timeout: 20_000, globalTimeout: 240_000,
  expect: { timeout: 5_000, toHaveScreenshot: { maxDiffPixels: 0, threshold: 0, animations: 'disabled', caret: 'hide' } },
  updateSnapshots: 'none',
  snapshotPathTemplate: resolve(root, foundation ? 'apps/web/components/foundation/tokens/testing/baselines/{platform}-{projectName}/{arg}{ext}' : 'apps/web/tests/fixtures/harness/baselines/{platform}-{projectName}/{arg}{ext}'),
  reporter: [[resolve(root, 'apps/web/tests/fixtures/harness/reporter.ts')]],
  // Pin software rendering and Skia's CPU path; cold GPU-backed runs varied corner rounding.
  use: { launchOptions: { args: ['--disable-gpu', '--disable-skia-runtime-opts'] }, browserName: 'chromium', headless: true, baseURL: origin, viewport: { width: 1440, height: 1100 }, deviceScaleFactor: 1, locale: 'en-US', timezoneId: 'UTC', colorScheme: 'light', reducedMotion: 'reduce', serviceWorkers: 'block', trace: 'on', screenshot: 'only-on-failure', video: 'off', actionTimeout: 5_000, navigationTimeout: 10_000 },
  projects: probe ? [{ name: 'probe', metadata: { mode: null } }] : [
    { name: 'customer_controlled', grepInvert: /@runner/, metadata: { mode: 'customer_controlled' } },
    { name: 'opentour_managed', grepInvert: /@runner/, metadata: { mode: 'opentour_managed' } },
    ...foundation ? [] : [{ name: 'runner', grep: /@runner/, metadata: { mode: null } }],
  ],
});
