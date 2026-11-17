import { expect, test } from '@playwright/test';
import type { OperationStateProps } from './operation-state.types';

const evaluation = {
  resource: 'Evaluation r1', observedAt: '09:40 UTC', kind: 'progress', state: 'complete',
  status: 'Evaluation completed', detail: 'Refund status · receipt-backed answers · policy v2',
} as const;
const hold = {
  resource: 'Candidate r1', observedAt: '09:41 UTC', kind: 'notice', tone: 'hold',
  status: 'Candidate on hold', detail: 'The incumbent continues serving; no promotion was attempted.',
} as const;
const incumbent = {
  resource: 'Incumbent r0', observedAt: '09:42 UTC', kind: 'badge', tone: 'success',
  status: 'Incumbent active', detail: '0% candidate traffic',
} as const;
const pass = { ...hold, tone: 'success', status: 'Pass', detail: 'Current policy permits the exact candidate and scope.' } as const;
const unknown = {
  resource: 'refund-status/v2', observedAt: '09:42 UTC', kind: 'data', state: 'unknown-outcome',
  status: 'Runtime unknown', detail: 'The serving identity is unconfirmed.',
} as const;
const requested = {
  resource: 'Promotion r1', observedAt: '09:40 UTC', kind: 'notice', tone: 'info',
  status: 'Promotion requested', detail: 'Promotion intent was accepted.',
} as const;
const staging = {
  resource: requested.resource, observedAt: requested.observedAt, kind: 'progress', state: 'running',
  status: 'Staging candidate', detail: 'The exact candidate is loading into a private replica.',
} as const;
const fixtures = {
  held: { operation: evaluation, decision: hold, runtime: incumbent },
  authorized: { operation: evaluation, decision: { ...pass, status: 'Authorized' }, runtime: incumbent },
  staging: { operation: staging, decision: pass, runtime: incumbent },
  accepted: { operation: requested, decision: pass, runtime: unknown },
  verifying: { operation: { ...staging, status: 'Verifying traffic', detail: 'Compare desired and observed route identity and probe evidence.' }, decision: pass, runtime: unknown },
  active: { operation: evaluation, decision: pass, runtime: { ...incumbent, resource: 'Candidate r1', status: 'Candidate active', detail: 'The observed runtime receipt confirms candidate r1 is serving in the authorized scope.' } },
  unknown: { operation: evaluation, decision: hold, runtime: unknown },
} satisfies Record<string, OperationStateProps>;

// Visible region heights from the editable compositions indexed in Figma guide 296:6.
// In each row the first four entries are at 100% and the remaining four are at 200%.
const heights = {
  held: [[212, 212, 166], [194, 194, 166], [194, 194, 166], [194, 194, 166], [400, 396, 258], [288, 288, 258], [288, 288, 258], [288, 288, 258]],
  authorized: [[212, 212, 166], [194, 194, 166], [194, 194, 166], [194, 194, 166], [400, 360, 258], [288, 288, 258], [288, 288, 258], [288, 288, 258]],
  staging: [[212, 212, 166], [194, 194, 166], [194, 194, 166], [194, 194, 166], [360, 360, 258], [288, 288, 258], [288, 288, 258], [288, 288, 258]],
  accepted: [[194, 212, 194], [194, 194, 194], [194, 194, 194], [194, 194, 194], [364, 360, 324], [288, 288, 288], [288, 288, 288], [288, 288, 288]],
  verifying: [[212, 212, 194], [194, 194, 194], [194, 194, 194], [194, 194, 194], [396, 360, 324], [324, 288, 288], [288, 288, 288], [288, 288, 288]],
  active: [[212, 212, 184], [194, 194, 166], [194, 194, 166], [194, 194, 166], [400, 360, 366], [288, 288, 294], [288, 288, 258], [288, 288, 258]],
  unknown: [[212, 212, 194], [194, 194, 194], [194, 194, 194], [194, 194, 194], [400, 396, 324], [288, 288, 288], [288, 288, 288], [288, 288, 288]],
} as const;

const widths = [320, 768, 1280, 1440] as const;
for (const name of Object.keys(fixtures) as (keyof typeof fixtures)[]) {
  for (const scale of [1, 2]) {
    for (const [index, width] of widths.entries()) {
      test(`${name} at ${width}px and ${scale * 100}% text`, async ({ page }, info) => {
        await page.setViewportSize({ width, height: 1100 });
        const fixture = fixtures[name];
        await page.goto('/operation-state?' + new URLSearchParams({ display: JSON.stringify(fixture) }));
        await page.locator('html').evaluate((el, value) => { el.style.fontSize = value; }, `${100 * scale}%`);
        await page.evaluate(() => document.fonts.ready);
        const root = page.getByRole('region', { name: 'Operation', exact: true }).locator('..');
        const expected = heights[name][index + (scale - 1) * widths.length];
        let y = 0;
        const bounds = await root.boundingBox();
        expect(bounds?.width).toBe(width);
        for (const [i, role] of (['operation', 'decision', 'runtime'] as const).entries()) {
          const section = root.getByRole('region', { name: role, exact: false });
          for (const copy of [fixture[role].resource, fixture[role].status, fixture[role].detail, 'Observed ' + fixture[role].observedAt]) {
            await expect(section.getByText(copy, { exact: true })).toBeVisible();
          }
          const box = await section.boundingBox();
          expect(box).toEqual({ x: bounds!.x, y: bounds!.y + y, width, height: expected[i] });
          y += expected[i] + 16;
        }
        await expect(root.getByRole('button')).toHaveCount(0);
        expect(await root.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
        const capture = `${name}-${width}-${scale}x.png`;
        const path = info.outputPath(capture);
        await root.screenshot({ path });
        await info.attach(capture, { path, contentType: 'image/png' });
      });
    }
  }
}
