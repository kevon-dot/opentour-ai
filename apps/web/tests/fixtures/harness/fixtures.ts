import { test as base, expect, type BrowserContext, type Page } from '@playwright/test';
import { FIXTURE, type Mode, type Scenario } from './contracts.ts';

type Harness = { id: string; mode: Mode; open(scenario?: Scenario, role?: 'owner' | 'viewer'): Promise<void>; configure(input: Record<string, unknown>): Promise<void>; inspect(): Promise<{ effects: number; requests: number; held: number; revision: string; organization: string; operation: string | null }>; release(): Promise<void>; call(action: string, value: unknown): Promise<{ status: number; body: Record<string, unknown> }> };
async function local(path: string, value?: unknown) {
  const response = await fetch(`${process.env.OPENTOUR_FIXTURE_ORIGIN}${path}`, { method: value === undefined ? 'GET' : 'POST', headers: { 'content-type': 'application/json' }, body: value === undefined ? undefined : JSON.stringify(value) });
  return { status: response.status, body: await response.json() };
}
export async function guard(context: BrowserContext) {
  const violations: string[] = [];
  const origin = process.env.OPENTOUR_FIXTURE_ORIGIN!;
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) { violations.push('FORBIDDEN_NETWORK'); await route.abort('blockedbyclient'); }
    else await route.continue();
  });
  await context.routeWebSocket('**/*', socket => { violations.push('FORBIDDEN_WEBSOCKET'); socket.close(); });
  return violations;
}
export const test = base.extend<{ harness: Harness; network: string[] }>({
  network: [async ({ context }, use) => {
    const violations = await guard(context); await use(violations);
    const nodeViolations = (globalThis as Record<symbol, unknown>)[Symbol.for('opentour.network.violations')] as string[] | undefined;
    expect(violations, 'Unexpected browser egress').toEqual([]);
    expect(nodeViolations ?? [], 'Unexpected Node/API egress').toEqual([]);
  }, { auto: true }],
  harness: async ({ page, browser }, use, info) => {
    const mode = info.project.metadata.mode as Mode;
    let id = '';
    const h: Harness = {
      get id() { return id; }, mode,
      async open(scenario = 'success', role = 'owner') {
        if (id) await local(`/__harness/${id}/destroy`, {});
        const created = await local('/__harness/session', { mode, scenario, role }); id = created.body.id;
        await page.clock.setFixedTime(new Date(FIXTURE.clock));
        await page.goto(`/harness?session=${id}`);
        await page.evaluate(async () => { await document.fonts.ready; });
        await info.attach('fixture-metadata', { body: Buffer.from(JSON.stringify({ ...FIXTURE, mode, role, authority_revision: scenario === 'denied' ? null : FIXTURE.authority_revision, kind: 'simulated', browser_version: browser.version() })), contentType: 'application/json' });
      },
      async configure(input) { await local(`/__harness/${id}/configure`, input); },
      async inspect() { return (await local(`/__harness/${id}/inspect`)).body; },
      async release() { await local(`/__harness/${id}/release`, {}); },
      async call(action, value) { return local(`/control/${id}/${action}`, value); },
    };
    await use(h);
    if (id) await local(`/__harness/${id}/destroy`, {});
  },
});
export { expect };
export async function stable(page: Page) { await page.evaluate(async () => { await document.fonts.ready; }); }
