import { test, expect, stable, guard } from './fixtures/harness/fixtures.ts';
import { test as runnerTest } from '@playwright/test';
import { FIXTURE } from './fixtures/harness/contracts.ts';
import { validateTarget, exactSelector } from './fixtures/harness/selection.ts';
import { spawn } from 'node:child_process';
import { mkdtempSync, realpathSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomBytes, createHash } from 'node:crypto';

test('BH-STATES: loading, empty, pending, unknown, denied, success and recovery', async ({ page, harness }) => {
  await harness.open('loading'); await expect(page.getByRole('status')).toHaveText('Loading');
  await harness.configure({ scenario: 'success' }); await harness.release();
  await page.getByRole('button', { name: 'Refresh status' }).click(); await expect(page.getByRole('status')).toHaveText('Ready');
  for (const [scenario, label] of [['empty','No items'], ['pending','Waiting for cells'], ['unknown','Unknown'], ['denied','Access denied'], ['success','Ready']] as const) {
    await harness.configure({ scenario }); await page.getByRole('button', { name: 'Refresh status' }).click(); await expect(page.getByRole('status')).toHaveText(label);
    if (scenario !== 'success') await expect(page.getByRole('button', { name: 'Save change' })).toBeDisabled();
  }
});
test('BH-TENANT-RACE: delayed old tenant never returns after switching', async ({ page, harness }) => {
  await harness.open(); await expect(page.getByRole('status')).toHaveText('Ready');
  await harness.configure({ delayed: true }); await page.getByRole('button', { name: 'Refresh status' }).click();
  await expect.poll(async () => (await harness.inspect()).held).toBe(1);
  await page.getByRole('button', { name: 'Switch to workspace B' }).click();
  await expect(page.locator('#organization')).toHaveText('Generated workspace B');
  await harness.release(); await page.waitForLoadState('networkidle');
  await expect(page.locator('body')).not.toContainText('Generated workspace A');
  await page.getByRole('button', { name: 'Save change' }).click();
  await expect.poll(async () => (await harness.inspect()).effects).toBe(1);
  expect((await harness.inspect()).organization).toBe('simulated-org-b');
});
test('BH-AUTHORITY: current server denial overrides enabled controls; failed switch grants nothing', async ({ page, harness }) => {
  await harness.open(); await expect(page.getByRole('button', { name: 'Save change' })).toBeEnabled();
  await harness.configure({ authority: false }); await page.getByRole('button', { name: 'Save change' }).click();
  await expect(page.getByRole('alert')).toContainText('Current authority does not permit'); expect((await harness.inspect()).effects).toBe(0);
  await page.getByRole('button', { name: 'Switch to workspace B' }).click(); await expect(page.getByRole('alert')).toContainText('Resource unavailable');
  await expect(page.locator('#authority')).toHaveText('Unknown'); await expect(page.locator('body')).not.toContainText('Generated workspace B');
});
test('BH-REVOCATION: HTTP202 remains pending and refresh retains operation', async ({ page, harness }) => {
  await harness.open(); await expect(page.getByRole('button', { name: 'Revoke access' })).toBeEnabled();
  await page.getByRole('button', { name: 'Revoke access' }).click(); await expect(page.getByRole('status')).toHaveText('Waiting for cells');
  await expect(page.locator('#detail')).toContainText('1 of 2 cells'); await expect(page.locator('#detail')).toContainText('simulated-revocation-127');
  await page.getByRole('button', { name: 'Refresh status' }).click(); await expect(page.getByRole('status')).toHaveText('Waiting for cells');
  expect((await harness.inspect()).effects).toBe(1);
  await expect(page.getByRole('button', { name: 'Retry same intent' })).toBeDisabled();
  const refused = await harness.call('mutate', { expected_revision: '9007199254740994', intent: 'simulated-after-revoke', value: 'new effect', resource: 'simulated-org-a' });
  expect(refused.status).toBe(403); expect((await harness.inspect()).effects).toBe(1);
});
test('BH-REVOCATION: lost revoke acknowledgment cannot enable a mutation retry', async ({ page, harness }) => {
  await harness.open(); await expect(page.getByRole('button', { name: 'Revoke access' })).toBeEnabled();
  await page.route('**/control/*/revoke', async route => { await route.fetch(); await route.abort('failed'); }, { times: 1 });
  await page.getByRole('button', { name: 'Revoke access' }).click();
  await expect(page.getByRole('status')).toHaveText('Unknown');
  await expect(page.getByRole('button', { name: 'Retry same intent' })).toBeDisabled();
  expect((await harness.inspect()).effects).toBe(1);
  await page.getByRole('button', { name: 'Refresh status' }).click();
  await expect(page.getByRole('status')).toHaveText('Waiting for cells');
  await expect(page.locator('#detail')).toContainText('simulated-revocation-127');
  expect((await harness.inspect()).effects).toBe(1);
});
test('BH-REVISION: exact decimal strings, malformed revisions and stale draft preservation', async ({ page, harness }) => {
  await harness.open(); await expect(page.locator('#revision')).toHaveText('9007199254740993');
  await page.getByLabel('Draft name').fill('Preserve my generated draft');
  await harness.configure({ scenario: 'stale' }); await page.getByRole('button', { name: 'Save change' }).click();
  await expect(page.getByRole('status')).toHaveText('Stale — review required'); await expect(page.getByLabel('Draft name')).toHaveValue('Preserve my generated draft'); expect((await harness.inspect()).effects).toBe(0);
  await harness.configure({ scenario: 'success', revision: '9223372036854775807' }); await page.getByRole('button', { name: 'Refresh status' }).click(); await expect(page.locator('#revision')).toHaveText('9223372036854775807');
  for (const invalid of [1, '01', '0', '-1', '1.0', '1e3', ' 1', '9223372036854775808']) {
    const response = await harness.call('mutate', { expected_revision: invalid, intent: 'simulated-invalid', value: 'x', resource: 'simulated-org-a' }); expect(response.status).toBe(422); expect(response.body.code).toBe('SCHEMA_INVALID');
  }
  const foreign = await harness.call('mutate', { expected_revision: '9223372036854775807', intent: 'simulated-foreign', value: 'x', resource: 'simulated-foreign-org' }); expect(foreign.status).toBe(404); expect(foreign.body.current_revision).toBeNull();
  expect((await harness.inspect()).effects).toBe(0);
});
test('BH-REPLAY: same intent has one effect; changed body conflicts; lost acknowledgment stays unknown', async ({ page, harness }) => {
  await harness.open(); await expect(page.getByRole('status')).toHaveText('Ready');
  await harness.configure({ scenario: 'lost_ack' }); await page.getByRole('button', { name: 'Save change' }).click();
  await expect(page.getByRole('status')).toHaveText('Unknown'); expect((await harness.inspect()).effects).toBe(1);
  await page.getByRole('button', { name: 'Retry same intent' }).click(); await expect(page.getByRole('status')).toHaveText('Unknown'); expect((await harness.inspect()).effects).toBe(1);
  const reordered = await harness.call('mutate', { resource: 'simulated-org-a', value: 'Generated draft', intent: 'simulated-intent-1', expected_revision: '9007199254740993' });
  expect(reordered.status).toBe(200); expect((await harness.inspect()).effects).toBe(1);
  const changedAction = await harness.call('revoke', { expected_revision: '9007199254740993', intent: 'simulated-intent-1', value: 'Generated draft', resource: 'simulated-org-a' });
  expect(changedAction.status).toBe(409); expect(changedAction.body.code).toBe('IDEMPOTENCY_CONFLICT'); expect((await harness.inspect()).operation).toBeNull();
  const conflict = await harness.call('mutate', { expected_revision: '9007199254740993', intent: 'simulated-intent-1', value: 'changed', resource: 'simulated-org-a' }); expect(conflict.status).toBe(409); expect(conflict.body.code).toBe('IDEMPOTENCY_CONFLICT');
});
test('BH-CELL-OFFLINE and BH-ERROR: actionable help, safe text, malformed payload and 429/503', async ({ page, harness }) => {
  await harness.open('offline'); await expect(page.getByRole('alert')).toContainText('private network');
  await page.getByRole('button', { name: 'Connection help' }).click(); await expect(page.getByRole('dialog')).toContainText('not proxied'); await page.getByRole('button', { name: 'Close help' }).click();
  for (const scenario of ['error','malformed','rate_limit','unavailable'] as const) {
    await harness.configure({ scenario }); await page.getByRole('button', { name: 'Refresh status' }).click(); await expect(page.getByRole('alert')).not.toBeEmpty();
    await expect(page.getByRole('button', { name: 'Save change' })).toBeDisabled(); await expect(page.locator('img')).toHaveCount(0); await expect(page.locator('body')).not.toContainText('simulated-private-diagnostic');
    if (scenario === 'error') await expect(page.getByRole('alert')).toContainText('<img src=x onerror=alert(1)>');
  }
  await harness.configure({ scenario: 'success' }); await page.getByRole('button', { name: 'Refresh status' }).click(); await expect(page.getByRole('status')).toHaveText('Ready');
});
test('BH-A11Y: keyboard focus, dialog recovery and disabled action', async ({ page, harness }) => {
  await harness.open('success', 'viewer'); await expect(page.getByRole('status')).toHaveText('Ready');
  await expect(page.getByRole('button', { name: 'Save change' })).toBeDisabled();
  await page.keyboard.press('Tab'); await expect(page.getByRole('button', { name: 'Workspace', exact: true })).toBeFocused();
  expect(await page.getByRole('button', { name: 'Workspace', exact: true }).evaluate(el => getComputedStyle(el).outlineStyle)).toBe('solid');
  await page.getByRole('button', { name: 'Connection help' }).focus(); await page.keyboard.press('Enter'); await expect(page.getByRole('dialog')).toBeVisible();
  await page.keyboard.press('Escape'); await expect(page.getByRole('dialog')).not.toBeVisible(); await expect(page.getByRole('button', { name: 'Connection help' })).toBeFocused();
  expect((await harness.inspect()).effects).toBe(0);
});
test('BH-RESET: account and storage state start isolated', async ({ page, harness, browser }) => {
  await harness.open(); await page.evaluate(() => { localStorage.setItem('simulated-prior-account', 'old'); sessionStorage.setItem('simulated-prior-organization', 'old'); });
  const isolated = await browser.newContext(); const violations = await guard(isolated);
  try { const next = await isolated.newPage(); await next.goto(`${process.env.OPENTOUR_FIXTURE_ORIGIN}/__harness/ready`); expect(await next.evaluate(() => [localStorage.length, sessionStorage.length])).toEqual([0,0]); expect(violations).toEqual([]); } finally { await isolated.close(); }
});
test('BH-DETERMINISM, BH-RESPONSIVE and BH-BILLING: reviewed zero-pixel route baselines', async ({ page, harness }, info) => {
  const root = process.env.OPENTOUR_REPOSITORY_ROOT!;
  const baseline = JSON.parse(readFileSync(join(root,'apps/web/tests/fixtures/harness/baseline-review.json'),'utf8'));
  const authoring = !!process.env.OPENTOUR_BASELINE_REVIEW_REASON;
  if (!authoring) {
    expect(baseline.review_status).toBe('APPROVED');
    expect(baseline.platform).toBe(process.platform);
    expect(baseline.architecture).toBe(process.arch);
  }
  // Start the cold document at the first reviewed viewport; resizing its initial wide
  // paint can retain a different rounded-border raster in pinned Chromium.
  await page.setViewportSize({ width: 320, height: 1100 });
  await harness.open(); await expect(page.getByRole('status')).toHaveText('Ready');
  expect(await page.evaluate(() => new Date().toISOString())).toBe(FIXTURE.clock);
  for (const width of [320,768,1280,1440]) {
    await page.setViewportSize({ width, height: 1100 });
    await page.getByRole('button', { name: 'Connection help' }).scrollIntoViewIfNeeded();
    await expect(page.getByRole('button', { name: 'Connection help' })).toBeInViewport();
    await page.evaluate(() => scrollTo(0, 0));
    for (const route of ['Workspace','Billing']) {
      await page.getByRole('button', { name: route, exact: true }).click(); await stable(page);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      if (route === 'Billing') await expect(page.locator('#billing-data')).toHaveText('Simulated amount: $12.50 USD · quantity: 12.50');
      const name = `${route.toLowerCase()}-${width}.png`;
      const path = info.snapshotPath(name);
      const baselineKey = path.slice(join(root,'apps/web/tests/fixtures/harness').length + 1);
      if (!authoring) expect(createHash('sha256').update(readFileSync(path)).digest('hex')).toBe(baseline.images[baselineKey]);
      await expect(page).toHaveScreenshot(name);
    }
  }
  await harness.configure({ scenario: 'unknown' }); await page.getByRole('button', { name: 'Refresh status' }).click(); await expect(page.locator('#billing-data')).toHaveText('Simulated amount: unknown · quantity: unknown');
});

runnerTest('BH-DISPATCH @runner: exact selection rejects unsafe paths and substring collisions', async () => {
  const root = process.env.OPENTOUR_REPOSITORY_ROOT!;
  for (const target of ['../outside.spec.ts','/tmp/outside.spec.ts','apps/web/tests/../outside.spec.ts','--help','apps/web/tests/x;touch.spec.ts','apps/web/tests/x.test.ts']) expect(() => validateTarget(root,target)).toThrow();
  const selector = new RegExp(exactSelector(`${root}/probe with spaces.spec.ts`,false));
  expect(selector.test(`${root}/probe with spaces.spec.ts`)).toBe(true); expect(selector.test(`${root}/not-probe with spaces.spec.ts`)).toBe(false); expect(selector.test(`${root}/probe with spaces.spec.ts.more`)).toBe(false);
  const directory = mkdtempSync(join(root,'apps/web/tests/fixtures/harness/probes/path-'));
  try { symlinkSync('/tmp',join(directory,'escape.spec.ts')); expect(() => validateTarget(root,`${directory.slice(root.length+1)}/escape.spec.ts`,true)).toThrow(); } finally { rmSync(directory,{recursive:true,force:true}); }
});
for (const [probe, expectedCode, reason] of [
  ['pass',0,null],['post_report_interrupt',1,'RUNNER_ERROR'],['directory',0,null],['fail',1,'ASSERTION_FAILED'],['zero',2,'NO_TESTS'],['skip',1,'REQUIRED_TEST_SKIPPED'],['expected_failure',1,'ASSERTION_FAILED'],['network',1,'ASSERTION_FAILED'],['missing_browser',2,'MISSING_BROWSER'],['missing_application',2,'MISSING_APPLICATION'],['timeout',1,'RUNNER_TIMEOUT'],['interrupt',1,'RUNNER_INTERRUPTED'],['cleanup',1,'CLEANUP_FAILED'],
] as const) {
  runnerTest(`BH-LIFECYCLE @runner: real subprocess ${probe}`, async () => {
    runnerTest.setTimeout(45_000);
    const root = process.env.OPENTOUR_REPOSITORY_ROOT!; const directory = realpathSync(mkdtempSync(join(tmpdir(),'opentour-probe-'))); const destination = join(directory,'report.json');
    try {
      const nonce = randomBytes(16).toString('hex');
      const child = spawn(process.execPath,[join(root,'apps/web/tests/fixtures/harness/probe-driver.ts'),root,destination,nonce,probe],{cwd:root,env:{PATH:process.env.PATH,HOME:directory,TMPDIR:directory},stdio:['ignore','pipe','pipe']});
      let diagnostic = ''; child.stdout.on('data',chunk => { diagnostic += chunk; }); child.stderr.on('data',chunk => { diagnostic += chunk; });
      const code = await new Promise(resolve => child.once('close',resolve));
      expect(code,diagnostic).toBe(expectedCode);
      const report = JSON.parse(readFileSync(destination,'utf8')); expect(report.reason_code).toBe(reason); expect(report.binding_nonce).toBe(nonce); expect(report.cleanup_complete).toBe(probe !== 'cleanup');
      if (probe === 'pass') { expect(report.collected).toBe(1); expect(report.tests[0].file).toContain('probe with spaces.spec.ts'); }
      if (probe === 'interrupt') { expect(report.collected).toBe(1); expect(report.failed).toBe(1); expect(JSON.parse(readFileSync(`${destination}.ready`, 'utf8')).nonce).toBe(nonce); }
    } finally { rmSync(directory,{recursive:true,force:true}); }
  });
}
runnerTest('BH-TRANSPORT @runner: stale identity and existing report cannot execute', async () => {
  const root = process.env.OPENTOUR_REPOSITORY_ROOT!; const directory = realpathSync(mkdtempSync(join(tmpdir(),'opentour-identity-'))); const destination = join(directory,'report.json');
  try {
    writeFileSync(destination,'sentinel');
    const target='apps/web/tests/browser-harness.browser.spec.ts';
    const child=spawn(process.execPath,[join(root,'tools/browser-test-runner.ts'),target,'--result-file',destination],{cwd:root,env:{PATH:process.env.PATH,OPENTOUR_REPOSITORY_ROOT:root,OPENTOUR_RESULT_FILE:destination,OPENTOUR_TEST_TARGET:'wrong-target',OPENTOUR_RESULT_NONCE:'a'.repeat(32)},stdio:'ignore'});
    expect(await new Promise(resolve=>child.once('close',resolve))).toBe(2); expect(readFileSync(destination,'utf8')).toBe('sentinel');
  } finally {rmSync(directory,{recursive:true,force:true});}
});

for (const mode of ['timeout', 'crash']) {
  runnerTest(`BH-LIFECYCLE @runner: outer supervisor ${mode} reaps established browser`, async ({ browserName }, info) => {
    expect(browserName).toBe('chromium');
    runnerTest.setTimeout(20_000);
    const root = process.env.OPENTOUR_REPOSITORY_ROOT!;
    const child = spawn(join(root,'.venv/bin/python'),[join(root,'apps/web/tests/fixtures/harness/qualify_cleanup.py'),mode,process.execPath],{cwd:root,env:{PATH:process.env.PATH},stdio:['ignore','pipe','pipe']});
    let output=''; let errors='';child.stdout.on('data',chunk=>{output+=chunk;});child.stderr.on('data',chunk=>{errors+=chunk;});
    const code=await new Promise(resolve=>child.once('close',resolve));
    expect(code,errors+output).toBe(0);
    const result=JSON.parse(output);expect(result.same_group).toBe(true);expect(result.survivors).toEqual([]);expect(result.listener_closed).toBe(true);
    await info.attach('owned-process-cleanup',{body:Buffer.from(output),contentType:'application/json'});
  });
}
