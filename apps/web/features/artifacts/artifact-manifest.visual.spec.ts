import { expect, test } from '@playwright/test';
import { buildEvidenceFixture, captureEvidence, mountEvidenceFixture } from '../../tests/fixtures/presentation/evidence-capture.ts';
import { fixtureDigest, type ManifestFixture } from './artifact-manifest.fixtures';

let fixture: Awaited<ReturnType<typeof buildEvidenceFixture>>;
test.beforeAll(async () => { fixture = await buildEvidenceFixture('artifact-manifest'); });

for (const width of [320, 768, 1280, 1440]) for (const scale of [100, 200]) {
  test(`artifact manifest at ${width}px and ${scale}% text`, async ({ page }, info) => {
    await mountEvidenceFixture(page, fixture, width, scale);
    const main = page.getByRole('main');
    const show = (state: ManifestFixture) => page.evaluate(value => window.showArtifactManifest(value), state);
    const capture = (state: string) => captureEvidence(page, info, `artifact-${state}-${width}-${scale}`);
    await show('model');
    await expect(main.getByRole('heading', { name: 'Route qualification' })).toBeVisible();
    const narrow = width - (width === 320 ? 32 : width === 768 ? 48 : 64) < 480 * scale / 100;
    const manifest = main.getByRole(narrow ? 'list' : 'table', { name: 'Artifact manifest' });
    const checks = main.getByRole(narrow ? 'list' : 'table', { name: 'Route qualification' });
    await expect(manifest).toContainText('fixture-rights-commercial-02 / evaluation');
    await expect(manifest).toContainText('fixture-checkpoint-02');
    if (!narrow) {
      await expect(manifest.getByRole('columnheader')).toHaveText(['Field', 'Current value']);
      await expect(checks.getByRole('columnheader')).toHaveText(['Check', 'Result']);
    }
    for (const [label, result] of [['Runtime profile compatibility', 'Qualified'], ['Artifact integrity', 'Passed'],
      ['License / purpose', 'Current'], ['Serving registration', 'Not performed']]) {
      await expect(checks.getByRole(narrow ? 'listitem' : 'row').filter({ hasText: label })).toContainText(result);
    }
    await expect(main.getByText(fixtureDigest, { exact: true })).toBeVisible();
    await capture('model');
    await page.keyboard.press('Tab');
    await expect(main.getByRole('link', { name: 'View serving identity' })).toBeFocused();
    await page.keyboard.press('Tab');
    const copy = main.getByRole('button', { name: 'Copy runtime digest' });
    await expect(copy).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(main.getByRole('status')).toHaveText('Digest copied.');
    expect(await page.evaluate(() => window.manifestWrites)).toEqual([fixtureDigest]);
    await capture('copy-focus');
    await show('rule');
    await expect(manifest).toContainText('Not required for rule repair');
    await expect(manifest).toContainText('fixture-rights-rule-01 / repair');
    await expect(main.getByRole('button')).toHaveCount(0);
    await capture('rule');
    await show('missing');
    await expect(manifest).toContainText('Unavailable');
    await expect(manifest).not.toContainText('Not required');
    await capture('missing');
    await show('error');
    await expect(manifest).toContainText('Checkpoint could not be read');
    await expect(checks).not.toContainText('License / purpose');
    await capture('error');
    await show('model');
    await expect(copy).toBeVisible();
    await show('restricted');
    await expect(main.getByText('Access unavailable')).toBeVisible();
    for (const surface of [await main.innerHTML(), await main.ariaSnapshot()]) {
      expect(surface).not.toMatch(/fixture-rights|fixture-checkpoint|sha256:|Copy runtime digest|fixture-serving|Qualified/);
    }
    await capture('restricted');
  });
}

test('copy digest remains keyboard accessible in forced colors', async ({ page }, info) => {
  await page.emulateMedia({ forcedColors: 'active' });
  await mountEvidenceFixture(page, fixture, 320, 200);
  await page.evaluate(() => window.showArtifactManifest('model'));
  const copy = page.getByRole('button', { name: 'Copy runtime digest' });
  await expect(copy).toBeVisible();
  await page.keyboard.press('Tab');
  await page.keyboard.press('Tab');
  await expect(copy).toBeFocused();
  await captureEvidence(page, info, 'artifact-forced-focus');
});
