import { exactSelector, validateTarget } from '../apps/web/tests/fixtures/harness/selection.ts';
import { spawn, execFileSync } from 'node:child_process';
import { existsSync, lstatSync, realpathSync, readFileSync, writeFileSync, mkdirSync, mkdtempSync, rmSync, linkSync, unlinkSync, readdirSync } from 'node:fs';
import { resolve, dirname, relative, join } from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { createHash, randomBytes } from 'node:crypto';

type Mode = 'customer_controlled' | 'opentour_managed' | null;
type Case = { id: string; file: string; status: 'passed' | 'failed' | 'skipped'; mode: Mode };
type Report = { schema_version: string; runner: string; target: string; binding_nonce: string; kind: string; mode: Mode; collected: number; passed: number; failed: number; skipped: number; tests: Case[]; status: 'PASS' | 'FAIL'; reason_code: string | null; cleanup_complete: boolean };
export type Probe = 'post_report_interrupt' | 'directory' | 'hold' | 'pass' | 'fail' | 'zero' | 'skip' | 'expected_failure' | 'timeout' | 'interrupt' | 'cleanup' | 'network' | 'missing_browser' | 'missing_application';
const harness = 'apps/web/tests/fixtures/harness';
const foundationRoot = 'apps/web/components/foundation/tokens';
const foundationTarget = `${foundationRoot}/visual.spec.ts`;
const sha = (bytes: string | Buffer) => createHash('sha256').update(bytes).digest('hex');
function validateDestination(path: string) {
  if (!path || !existsSync(dirname(path)) || lstatSync(dirname(path)).isSymbolicLink() || realpathSync(dirname(path)) !== dirname(path) || existsSync(path)) throw new Error('INVALID_CONFIGURATION');
}
function atomicReport(path: string, report: Report) {
  validateDestination(path);
  const temp = `${path}.${randomBytes(8).toString('hex')}.tmp`;
  try { writeFileSync(temp, JSON.stringify(report), { flag: 'wx', mode: 0o600 }); linkSync(temp, path); } finally { if (existsSync(temp)) unlinkSync(temp); }
}
function inventory(root: string, path: string, output: Array<{ path: string; sha256: string }> = []) {
  for (const item of readdirSync(path, { withFileTypes: true })) { const full = join(path, item.name); if (item.isSymbolicLink()) throw new Error('artifact_symlink'); if (item.isDirectory()) inventory(root, full, output); else if (item.isFile()) output.push({ path: relative(root, full), sha256: sha(readFileSync(full)) }); }
  return output;
}
export function collectVisualComparisons(root: string, outputs: Array<{ path: string; sha256: string }>) {
  const names = /\/(billing320-(?:comparison|adverse-(?:position|border-color|radius|text)))\.json$/;
  const records: Array<{ path: string; sha256: string; png_path: string; png_sha256: string; comparison: Record<string, unknown> }> = [];
  const seen = new Set<string>();
  for (const output of outputs) {
    const match = names.exec(output.path); if (!match) continue;
    const bytes = readFileSync(join(root, output.path));
    if (bytes.length > 4096 || sha(bytes) !== output.sha256) throw new Error('RUNNER_ERROR');
    const value = JSON.parse(bytes.toString('utf8')) as Record<string, unknown>;
    const positive = match[1] === 'billing320-comparison';
    const expectedKeys = positive ? ['mode','policy_sha256','baseline_sha256','actual_sha256','variant','changed_pixels','bounds'] : ['mode','expected_rejection','actual_sha256','variant','changed_pixels','bounds'];
    const pngPath = output.path.replace(/\.json$/, '.png'); const png = outputs.find(item => item.path === pngPath);
    const identity = `${value.mode}:${match[1]}`;
    if (Object.keys(value).sort().join() !== expectedKeys.sort().join() || !['customer_controlled','opentour_managed'].includes(value.mode as string) || seen.has(identity) || !png || png.sha256 !== value.actual_sha256 || !Number.isInteger(value.changed_pixels) || Number(value.changed_pixels) < 0 || Number(value.changed_pixels) > 352000) throw new Error('RUNNER_ERROR');
    if (positive) {
      const policyBytes = readFileSync(join(root, harness, 'visual-comparison-policy.json'));
      const policy = JSON.parse(policyBytes.toString('utf8'));
      const baselineKey = `baselines/darwin-${String(value.mode).replaceAll('_','-')}/billing-320.png`;
      if (value.policy_sha256 !== sha(policyBytes) || value.baseline_sha256 !== policy.images[baselineKey]?.baseline_sha256 || !['baseline','reviewed_corner_vector',null].includes(value.variant as string | null)) throw new Error('RUNNER_ERROR');
      if ((value.variant === 'baseline' && value.changed_pixels !== 0) || (value.variant === 'reviewed_corner_vector' && value.changed_pixels !== 14)) throw new Error('RUNNER_ERROR');
    } else if (value.expected_rejection !== true || value.variant !== null || Number(value.changed_pixels) === 0) throw new Error('RUNNER_ERROR');
    if (value.bounds !== null) {
      const bounds = value.bounds as Record<string, unknown>;
      if (Object.keys(bounds).sort().join() !== 'max_x,max_y,min_x,min_y' || Object.values(bounds).some(number => !Number.isInteger(number) || Number(number) < 0) || Number(bounds.max_x) >= 320 || Number(bounds.max_y) >= 1100 || Number(bounds.min_x) > Number(bounds.max_x) || Number(bounds.min_y) > Number(bounds.max_y)) throw new Error('RUNNER_ERROR');
    } else if (value.changed_pixels !== 0) throw new Error('RUNNER_ERROR');
    seen.add(identity); records.push({ ...output, png_path: pngPath, png_sha256: png.sha256, comparison: value });
  }
  return records;
}
export function foundationMetadata(root: string, baselineReview?: { reason: string; designRevision: string }): Record<string, unknown> {
  const fontManifest = readFileSync(join(root, 'apps/web/styles/fonts/source.json'));
  const declaredFonts = JSON.parse(fontManifest.toString('utf8')).fonts;
  const fonts = ['Geist', 'GeistMono'].map(family => {
    const path = `apps/web/styles/fonts/${family}.woff2`;
    const digest = sha(readFileSync(join(root, path)));
    if (!Array.isArray(declaredFonts) || declaredFonts.filter(item => item.family === family && item.sha256 === digest).length !== 1) throw new Error('INVALID_CONFIGURATION');
    return { family, path, sha256: digest };
  });
  const tokenManifest = readFileSync(join(root, 'apps/web/styles/product-tokens.json'));
  const design = JSON.parse(tokenManifest.toString('utf8')).design;
  const reviewBytes = readFileSync(join(root, foundationRoot, 'testing/baseline-review.json'));
  const review = JSON.parse(reviewBytes.toString('utf8'));
  if (review.issue_key !== 'OPE-253' || review.profile_id !== 'c4865bd5-0fce-4468-b6ea-05003ebfa5a2' ||
    review.max_diff_pixels !== 0 || review.threshold !== 0 || !Array.isArray(review.masks) || review.masks.length ||
    review.design_amendment?.ledger_sha256 !== design.focus_amendment_ledger_sha256 ||
    review.design_amendment?.image_sha256 !== design.focus_amendment_image_sha256 ||
    review.design_review_status !== 'APPROVED_NARROW_FOCUS_AMENDMENT') throw new Error('INVALID_CONFIGURATION');
  if (baselineReview) {
    if (!baselineReview.reason || baselineReview.designRevision !== review.design_revision) throw new Error('INVALID_CONFIGURATION');
  } else {
    if (review.browser_baseline_status !== 'APPROVED' || !review.images || typeof review.images !== 'object') throw new Error('INVALID_CONFIGURATION');
    const names = ['gallery-320', 'gallery-768', 'gallery-1280', 'gallery-1440', 'focus-primary', 'focus-secondary', 'focus-quiet', 'focus-forced-colors'];
    for (const mode of ['customer-controlled', 'opentour-managed']) for (const name of names) {
      const key = `testing/baselines/${process.platform}-${mode}/${name}.png`;
      const file = join(root, foundationRoot, key);
      if (!existsSync(file) || lstatSync(file).isSymbolicLink() || review.images[key] !== sha(readFileSync(file))) throw new Error('INVALID_CONFIGURATION');
    }
  }
  return {
    issue_key: 'OPE-253', requirement_ids: ['OT-P23-C12-AC01', 'OT-P23-C12-AC02', 'OT-P23-C12-AC06', 'OT-P23-C12-AC07', 'OT-P23-C12-AC08', 'OT-P23-C12-AC09', 'OT-P23-C12-AC10'],
    source_refs: ['OPE-253', 'c4865bd5-0fce-4468-b6ea-05003ebfa5a2'], fixture_id: 'presentation-foundation-v1', seed: 253,
    spec_sha256: '968ff7c59d40afa568e5dfd0ea2488a8906531b38bb3daa00b66c58d8a7f8264',
    native_specification: {
      digest_recipe: 'SHA256 UTF-8 JSON sorted keys, compact separators, ASCII escaping: {issue_description, implementation_profile:{id,content}}',
      issue_description_sha256: '6a0b7ab8300e2e4d84a40d4a417f4b27cf6160bb37595604a31489977f6ba079',
      issue_updated_at: '2026-11-16T02:32:43.780Z',
      implementation_profile_id: 'c4865bd5-0fce-4468-b6ea-05003ebfa5a2',
      implementation_profile_sha256: 'b6c396ed2f3a038c6229b597769ebce03877c7a2ef7ecf22a479eaee6bb390f3',
      implementation_profile_updated_at: '2026-11-16T02:23:29.803Z',
    }, fonts, viewport_sizes: [{ width: 320, height: 800 }, { width: 768, height: 1024 }, { width: 1280, height: 900 }, { width: 1440, height: 1024 }],
    font_manifest_sha256: sha(fontManifest), token_manifest_sha256: sha(tokenManifest), design,
    baseline_manifest_sha256: sha(reviewBytes),
    presentation_sources: inventory(root, join(root, foundationRoot)).filter(item => !item.path.includes('/testing/baselines/')),
    visual_comparisons: [], limitations: ['Partial reusable presentation foundations only; no complete OPE-253 AC acceptance.', 'No domain API, authority, intent, receipt, provider or application-flow qualification.', 'Manual assistive-technology review and remaining primitives NOT_RUN.'],
  };
}
const overlaySpecification = {
  issue_key: 'OPE-300', logical_key: 'OT-P23-C12-OVERLAY',
  spec_sha256: '429990397f6a755551664d50ad0c80116abf65be8ac8e64e35002cbc18923fdf',
  native_specification: {
    digest_recipe: 'SHA256 UTF-8 JSON sorted keys, compact separators, ASCII escaping: {issue_description, implementation_profile:{id,content}}; exact native strings',
    issue_description_sha256: '8ff8e1c004bc6f6e8fa33f00181337ea82bb6d58b537280b996bd40312a36142',
    issue_updated_at: '2026-11-16T04:28:36.979Z',
    implementation_profile_id: 'c4865bd5-0fce-4468-b6ea-05003ebfa5a2',
    implementation_profile_sha256: '323f5919a4e396a4d844cf19f30c5fba2a88e8d9cffe3584405b1d86e2ffac9b',
    implementation_profile_updated_at: '2026-11-16T04:28:36.062Z',
  },
};

function configuration(condition: unknown): asserts condition {
  if (!condition) throw new Error('INVALID_CONFIGURATION');
}
function ownedBytes(root: string, path: string, limit: number): Buffer {
  const absolute = join(root, path);
  configuration(realpathSync(absolute) === absolute && lstatSync(absolute).isFile() && lstatSync(absolute).size <= limit);
  return readFileSync(absolute);
}
function record(value: unknown): Record<string, unknown> {
  configuration(value !== null && typeof value === 'object' && !Array.isArray(value));
  return value as Record<string, unknown>;
}

/**
 * Finite child attribution: exact reviewed IDs, not title prefixes or a new outer profile.
 * `platform` names the baseline directory to verify. The browser runner always uses its own host,
 * which is also the platform Playwright compares against; only unit fixtures name one explicitly.
 */
export function overlayMetadata(root: string, observed?: ReadonlyArray<Pick<Case, 'id' | 'file' | 'mode'>>, platform: NodeJS.Platform = process.platform) {
  const manifest = JSON.parse(ownedBytes(root, 'docs/specification/manifest.json', 8 * 1024 * 1024).toString('utf8'));
  configuration(Array.isArray(manifest.test_bindings) && Array.isArray(manifest.source_projection?.issues));
  const child = manifest.source_projection.issues.filter((value: Record<string, unknown>) => value.issue_key === 'OPE-300');
  configuration(child.length === 1 && child[0].logical_key === overlaySpecification.logical_key &&
    child[0].technical_capture_sha256 === overlaySpecification.spec_sha256 &&
    child[0].native_uuid === 'b215de00-fc9e-497b-915f-3dc835614bfa' &&
    child[0].updated_at === overlaySpecification.native_specification.issue_updated_at &&
    child[0].publication_spec_sha256 === overlaySpecification.native_specification.issue_description_sha256);
  const groups = new Map<string, string[]>();
  for (const [owner, count, digest] of [
    ['OPE-253', 36, '146c046a366f26c6ad053dec8959798f8dc26aad1867f59c983df82ba359f0dd'],
    ['OPE-300', 122, '3db50f715db241a9cf1f926bd4064cb8003cfde54c4b56420aa0289b350fd37d'],
  ] as const) {
    const bindings = manifest.test_bindings.filter((value: Record<string, unknown>) => value.issue_key === owner &&
      typeof value.test_id === 'string' && value.test_id.startsWith(`${foundationTarget}::`));
    configuration(bindings.length === count);
    for (const binding of bindings) configuration(binding.kind === 'simulated' &&
      binding.fixture_id === 'bootstrap-command-contract' && binding.fixture_version === '1' &&
      ['customer_controlled', 'opentour_managed'].includes(binding.mode) &&
      binding.test_id.startsWith(`${foundationTarget}::${binding.mode}::`));
    const ids = bindings.map((binding: Record<string, unknown>) => binding.test_id as string).sort();
    configuration(new Set(ids).size === count && sha(JSON.stringify(ids)) === digest);
    groups.set(owner, ids);
  }
  const allIds = [...groups.get('OPE-253')!, ...groups.get('OPE-300')!].sort();
  configuration(new Set(allIds).size === 158);
  if (observed) {
    configuration(observed.length === 158 && new Set(observed.map(value => value.id)).size === 158 &&
      JSON.stringify(observed.map(value => value.id).sort()) === JSON.stringify(allIds));
    for (const value of observed) configuration(value.file === foundationTarget &&
      ['customer_controlled', 'opentour_managed'].includes(value.mode ?? '') &&
      value.id.startsWith(`${foundationTarget}::${value.mode}::`));
  }
  const bytes = ownedBytes(root, `${foundationRoot}/testing/overlay-baseline-review.json`, 32768);
  configuration(sha(bytes) === '7fbcd3ce1d2eb362f2d608efe9a9627407d278e9341aae7785fe78b2a63a5d71');
  const review = record(JSON.parse(bytes.toString('utf8')));
  configuration(Object.keys(review).sort().join() === ['schema_version', 'issue_key', 'logical_key', 'spec_sha256',
    'design_image_sha256', 'browser_baseline_status', 'max_diff_pixels', 'threshold', 'masks', 'images',
    'independent_browser_baseline_review'].sort().join());
  configuration(review.schema_version === 'opentour.overlay-baseline-review.v1' && review.issue_key === 'OPE-300' &&
    review.logical_key === overlaySpecification.logical_key && review.spec_sha256 === overlaySpecification.spec_sha256 &&
    review.design_image_sha256 === 'a048ee48055c5a3de9089331e41a9e91aa0d64106dde8f8e065a3bd2e093b79d' &&
    review.browser_baseline_status === 'APPROVED' && review.max_diff_pixels === 0 && review.threshold === 0 &&
    Array.isArray(review.masks) && review.masks.length === 0);
  const approval = record(review.independent_browser_baseline_review);
  configuration(Object.keys(approval).sort().join() === ['reviewer', 'source_commit', 'review_sha256', 'baseline_authoring_nonce'].sort().join() &&
    approval.reviewer === 'root-independent-Codex' && approval.source_commit === '7c68462c06101d727c2a1f88d3b37f545b16f169' &&
    approval.review_sha256 === '0370c59eb923acfdfeba0e875fef3f7e7c98a1d1ed2708347798ee8c5f864ad0' && approval.baseline_authoring_nonce === '3d93df6163434bbc9b87841a4f27cfcf');
  const images = record(review.images); const paths: string[] = [];
  for (const mode of ['customer-controlled', 'opentour-managed']) for (const kind of ['dialog', 'drawer']) for (const width of [320, 768, 1280, 1440]) {
    const path = `testing/baselines/${platform}-${mode}/overlay-${kind}-${width}.png`; paths.push(path);
    configuration(images[path] === sha(ownedBytes(root, `${foundationRoot}/${path}`, 4 * 1024 * 1024)));
  }
  configuration(Object.keys(images).sort().join() === paths.sort().join());
  return { ...overlaySpecification, test_ids: groups.get('OPE-300')!, baseline_manifest_sha256: sha(bytes) };
}

function descendants(pid: number): Map<number, string> {
  const rows = execFileSync('ps', ['-axo', 'pid=,ppid=,lstart='], { encoding: 'utf8', timeout: 2000 }).trim().split('\n').map(line => { const match = /^\s*(\d+)\s+(\d+)\s+(.+)$/.exec(line); return match ? { pid: Number(match[1]), parent: Number(match[2]), stamp: match[3] } : null; }).filter(row => row !== null);
  const result = new Map<number, string>(); let changed = true;
  while (changed) { changed = false; for (const row of rows) if ((row.parent === pid || result.has(row.parent)) && !result.has(row.pid)) { result.set(row.pid, row.stamp); changed = true; } }
  return result;
}
function alive(pid: number, stamp: string) { try { return execFileSync('ps', ['-p', String(pid), '-o', 'lstart='], { encoding: 'utf8', timeout: 2000 }).trim() === stamp; } catch { return false; } }
export async function executeBrowser(options: { root: string; target: string; destination: string; nonce: string; probe?: Probe; timeoutMs?: number; baselineReview?: { reason: string; designRevision: string } }) {
  const { root, target, destination, nonce, probe } = options;
  validateDestination(destination);
  const foundation = target === foundationTarget;
  if (!/^[a-f0-9]{32}$/.test(nonce)) throw new Error('INVALID_CONFIGURATION');
  const report: Report = { schema_version: 'opentour.test-result.v1', runner: 'playwright', target, binding_nonce: nonce, kind: 'simulated', mode: null, collected: 0, passed: 0, failed: 0, skipped: 0, tests: [], status: 'FAIL', reason_code: 'INVALID_CONFIGURATION', cleanup_complete: true };
  let code = 2;
  let presentationMetadata: Record<string, unknown> | undefined;
  let componentMetadata: ReturnType<typeof overlayMetadata> | undefined;
  let server: { origin: string; close(): Promise<boolean> } | undefined;
  let runDirectory = '';
  let artifactDirectory = '';
  let interrupted = false;
  let timedOut = false;
  let processCleanup = true;
  let applicationCleanup = true;
  let cleanupDiagnostic = '';
  let child: ReturnType<typeof spawn> | undefined;
  const known = new Map<number, string>();
  // Pinned Playwright's SIGINT handler drains workers and writes its interrupted report.
  const signal = () => { interrupted = true; child?.kill('SIGINT'); };
  process.on('SIGINT', signal); process.on('SIGTERM', signal);
  try {
    const selection = validateTarget(root, target, !!probe);
    if (foundation && probe || !foundation && !probe && target !== 'apps/web/tests/browser-harness.browser.spec.ts') throw new Error('INVALID_CONFIGURATION');
    const applicationRoot = join(root, foundation ? `${foundationRoot}/testing` : harness, probe === 'missing_application' ? 'absent-application' : '');
    const applicationFiles = foundation ? ['index.html', 'gallery.tsx', 'server.ts', 'baseline-review.json', '../index.tsx', '../../../../styles/fonts/Geist.woff2', '../../../../styles/fonts/GeistMono.woff2', '../../../../styles/fonts/source.json'] : ['index.html', 'client.ts', 'server.ts', 'contracts.ts', 'assets/inter-latin-400-normal.woff2'];
    if (applicationFiles.some(file => !existsSync(join(applicationRoot, file)))) throw new Error('MISSING_APPLICATION');
    if (foundation) {
      try { presentationMetadata = foundationMetadata(root, options.baselineReview); componentMetadata = overlayMetadata(root); }
      catch { throw new Error('INVALID_CONFIGURATION'); }
    }
    const browserDirectory = probe === 'missing_browser' ? join(dirname(destination), 'absent-browser-cache') : join(root, '.toolchain/playwright');
    process.env.PLAYWRIGHT_BROWSERS_PATH = browserDirectory;
    const require = createRequire(join(root, 'package.json'));
    const playwrightPackage = createRequire(require.resolve('@playwright/test')).resolve('playwright/package.json');
    const corePackage = createRequire(playwrightPackage).resolve('playwright-core/package.json');
    // Qualified against locked Playwright 1.65.1: headless with no channel selects this build.
    const { registry: { registry } } = require(join(dirname(corePackage), 'lib/coreBundle.js'));
    const executable = registry.findExecutable('chromium-headless-shell')?.executablePath();
    if (!executable || !existsSync(executable)) throw new Error('MISSING_BROWSER');
    runDirectory = mkdtempSync(join(dirname(destination), 'browser-'));
    mkdirSync(join(runDirectory, 'home'));
    artifactDirectory = join(root, foundation ? 'evidence/OT-P23-C12/runs' : 'evidence/OT-BOOT-C06/runs', nonce); mkdirSync(artifactDirectory, { recursive: true });
    const raw = join(runDirectory, 'raw.json');
    if (foundation) {
      const { startFoundationServer } = await import(pathToFileURL(join(root, foundationRoot, 'testing/server.ts')).href);
      server = await startFoundationServer(nonce, runDirectory);
    } else {
      const { startFixtureServer } = await import(pathToFileURL(join(root, harness, 'server.ts')).href);
      server = await startFixtureServer(nonce, probe === 'cleanup');
    }
    const ready = await fetch(`${server!.origin}/__harness/ready`, { signal: AbortSignal.timeout(3000) }).then(response => response.json());
    if (ready.run_id !== nonce) throw new Error('MISSING_APPLICATION');
    const environment: NodeJS.ProcessEnv = { PATH: process.env.PATH, HOME: join(runDirectory, 'home'), TMPDIR: runDirectory, CI: 'true', TZ: 'UTC', LANG: 'C.UTF-8', NEXT_TELEMETRY_DISABLED: '1', PLAYWRIGHT_BROWSERS_PATH: browserDirectory, OPENTOUR_REPOSITORY_ROOT: root, OPENTOUR_BROWSER_TARGET: target, OPENTOUR_BROWSER_PROFILE: foundation ? 'presentation-foundation-v1' : 'simulated-browser-harness', OPENTOUR_FIXTURE_ORIGIN: server!.origin, OPENTOUR_BROWSER_RAW: raw, OPENTOUR_BROWSER_OUTPUT: join(artifactDirectory, 'artifacts'), OPENTOUR_BROWSER_PROBE: probe, OPENTOUR_BASELINE_REVIEW_REASON: options.baselineReview?.reason, OPENTOUR_BROWSER_READY_FILE: `${destination}.ready`, OPENTOUR_RESULT_NONCE: nonce, NODE_OPTIONS: `--require=${JSON.stringify(join(root, harness, 'network-node.cjs'))}` };
    const playwright = join(dirname(createRequire(require.resolve('@playwright/test')).resolve('playwright/package.json')), 'cli.js');
    const argv = [playwright, 'test', '--config', join(root, 'playwright.config.ts'), exactSelector(selection.path, selection.directory)];
    if (options.baselineReview) { if (!options.baselineReview.reason || !options.baselineReview.designRevision || probe) throw new Error('INVALID_CONFIGURATION'); argv.push('--update-snapshots=all'); }
    child = spawn(process.execPath, argv, { cwd: root, env: environment, stdio: ['ignore', 'pipe', 'pipe'], detached: false });
    let outputBytes = 0; let log = '';
    const capture = (chunk: Buffer) => { outputBytes += chunk.length; if (outputBytes > 1_048_576) child?.kill('SIGTERM'); else log += chunk.toString(); };
    child.stdout?.on('data', capture); child.stderr?.on('data', capture);
    const track = setInterval(() => { if (!child?.pid) return; try { descendants(child.pid).forEach((stamp, pid) => known.set(pid, stamp)); } catch (error) { processCleanup = false; cleanupDiagnostic = error instanceof Error ? error.message : 'process_inspection'; } }, 200);
    const timeout = setTimeout(() => { timedOut = true; child?.kill('SIGTERM'); }, options.timeoutMs ?? 250_000);
    // Interrupt an established browser, not an arbitrary point in CLI startup.
    // The overall deadline still fails closed if the nonce-bound ready signal never arrives.
    const interrupt = probe === 'interrupt' ? setInterval(() => {
      const readyPath = `${destination}.ready`;
      if (!existsSync(readyPath)) return;
      try {
        const ready = JSON.parse(readFileSync(readyPath, 'utf8'));
        if (ready.nonce === nonce && ready.origin === server!.origin) { clearInterval(interrupt); signal(); }
      } catch { /* A partial readiness write is retried within the existing deadline. */ }
    }, 50) : undefined;
    let rawExit: number | null;
    try { rawExit = await new Promise<number | null>((resolveExit, reject) => { child!.once('error', reject); child!.once('close', resolveExit); }); }
    finally { clearInterval(track); clearTimeout(timeout); if (interrupt) clearInterval(interrupt); }
    // Only synthetic fixture logs are retained locally; no inherited credential/config environment.
    writeFileSync(join(artifactDirectory, 'runner.log'), log);
    // Do not signal sampled PIDs: only launch-time child handles and the outer owned group may be killed.
    await new Promise(resolveDelay => setTimeout(resolveDelay, 100));
    for (const [pid, stamp] of known) if (alive(pid, stamp)) { processCleanup = false; cleanupDiagnostic = `survivor:${pid}`; }
    if (existsSync(raw)) {
      const value = JSON.parse(readFileSync(raw, 'utf8'));
      if (!Array.isArray(value.tests)) throw new Error('RUNNER_ERROR');
      report.tests = value.tests;
      const seen = new Set<string>();
      for (const test of report.tests) {
        if (typeof test.id !== 'string' || seen.has(test.id) || !['passed', 'failed', 'skipped'].includes(test.status) || ![null, 'customer_controlled', 'opentour_managed'].includes(test.mode) || (test.file !== target && !(selection.directory && test.file.startsWith(`${target}/`)))) throw new Error('INVALID_CONFIGURATION');
        seen.add(test.id); report[test.status]++;
      }
      report.collected = report.tests.length;
      if (!report.collected) report.reason_code = value.noTests || !value.errors ? 'NO_TESTS' : 'INVALID_CONFIGURATION';
      else if (value.errors && report.failed === 0) report.reason_code = 'RUNNER_ERROR';
      else report.reason_code = report.failed ? 'ASSERTION_FAILED' : report.skipped ? 'REQUIRED_TEST_SKIPPED' : !report.collected ? 'NO_TESTS' : rawExit !== 0 || value.status !== 'passed' ? 'RUNNER_ERROR' : null;
    } else report.reason_code = 'RUNNER_ERROR';
    if (timedOut) report.reason_code = 'RUNNER_TIMEOUT';
    if (interrupted) report.reason_code = 'RUNNER_INTERRUPTED';
    if (outputBytes > 1_048_576) report.reason_code = 'RUNNER_ERROR';
    code = report.reason_code === null ? 0 : report.reason_code === 'NO_TESTS' || report.reason_code === 'INVALID_CONFIGURATION' ? 2 : 1;
  } catch (error) {
    const reason = error instanceof Error ? error.message : '';
    report.reason_code = ['MISSING_APPLICATION', 'MISSING_BROWSER', 'INVALID_CONFIGURATION'].includes(reason) ? reason : 'RUNNER_ERROR';
    code = report.reason_code === 'RUNNER_ERROR' ? 1 : 2;
  } finally {
    process.off('SIGINT', signal); process.off('SIGTERM', signal);
    if (server) { try { report.cleanup_complete = await server.close(); } catch { report.cleanup_complete = false; } }
    applicationCleanup = report.cleanup_complete;
    if (runDirectory) { try { rmSync(runDirectory, { recursive: true, force: true }); } catch { report.cleanup_complete = false; } }
    if (!processCleanup) report.cleanup_complete = false;
    if (!report.cleanup_complete) { code = 1; report.reason_code = 'CLEANUP_FAILED'; }
    report.status = code === 0 ? 'PASS' : 'FAIL';
    if (artifactDirectory) {
      try {
      let commit: string | null = null; let sourceDirty = true; try { commit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim(); sourceDirty = !!execFileSync('git', ['status', '--porcelain', '--untracked-files=normal'], { cwd: root, encoding: 'utf8' }).trim(); } catch { /* Evidence remains explicitly unbound. */ }
      const outputs = inventory(root, artifactDirectory);
      let visualComparisons: ReturnType<typeof collectVisualComparisons> = [];
      let visualEvidenceValid = true;
      try { visualComparisons = collectVisualComparisons(root, outputs); } catch { visualEvidenceValid = false; }
      if (!probe && !options.baselineReview && target === 'apps/web/tests/browser-harness.browser.spec.ts' && report.status === 'PASS' && (visualComparisons.length !== 10 || visualComparisons.some(record => record.path.endsWith('/billing320-comparison.json') && record.comparison.variant === null))) visualEvidenceValid = false;
      if (!visualEvidenceValid) { code = 1; report.status = 'FAIL'; report.reason_code ??= 'RUNNER_ERROR'; }
      const evidence: Record<string, unknown> = { schema_version: 'opentour.browser-artifacts.v1', issue_key: 'OPE-127', kind: 'simulated', requirement_ids: ['OT-BOOT-C06-AC01', 'OT-BOOT-C06-AC02'], source_refs: ['G1', 'AC-22-01', 'AC-23-01'], product_acceptance: 'NOT_RUN', commit_sha: commit, git_worktree_dirty: sourceDirty, test_source_sha256: sha(lstatSync(join(root, target)).isDirectory() ? JSON.stringify(inventory(root, join(root, target))) : readFileSync(join(root, target))), target, fixture_id: 'simulated-browser-harness', fixture_version: '1', seed: 127, clock: '2026-11-14T12:00:00.000Z', spec_sha256: '96c405228e6e925e3c11ea4d44c0d5cb5b984541bab1a017735e7629ffa72292', cleanup_observation: { processCleanup, applicationCleanup, cleanupDiagnostic }, baseline_review_request: options.baselineReview ?? null, platform: process.platform, architecture: process.arch, playwright_version: JSON.parse(readFileSync(join(root, 'package.json'), 'utf8')).devDependencies['@playwright/test'], viewport_widths: [320, 768, 1280, 1440], locale: 'en-US', timezone: 'UTC', color_scheme: 'light', ...foundation ? {} : { font_sha256: sha(readFileSync(join(root, harness, 'assets/inter-latin-400-normal.woff2'))) }, outputs, visual_comparisons: visualComparisons, report, limitations: ['Deterministic simulated interaction evidence only.', 'Provider/customer/isolation G1 acceptance NOT_RUN.', 'Canonical Linux visual qualification requires its platform baselines and independent review.'] };
      if (foundation) {
        const current = foundationMetadata(root, options.baselineReview);
        if (JSON.stringify(current) !== JSON.stringify(presentationMetadata)) {
          code = 1; report.status = 'FAIL'; report.reason_code = 'RUNNER_ERROR';
        }
        Object.assign(evidence, current);
        evidence.component_specifications = [componentMetadata];
        try {
          const component = overlayMetadata(root, report.tests);
          if (JSON.stringify(component) !== JSON.stringify(componentMetadata)) {
            code = 1; report.status = 'FAIL'; report.reason_code = 'RUNNER_ERROR';
          } else evidence.component_specifications = [component];
        } catch {
          // Keep the actual failed report/artifacts; invalid attribution cannot produce PASS.
          code = 1; report.status = 'FAIL'; report.reason_code = 'RUNNER_ERROR';
        }
      }
      writeFileSync(join(artifactDirectory, 'evidence.json'), JSON.stringify(evidence, null, 2));
      } catch { code = 1; report.status = 'FAIL'; report.reason_code = 'RUNNER_ERROR'; }
    }
    atomicReport(destination, report);
  }
  return code;
}
async function main() {
  const [target, option, destination, ...extra] = process.argv.slice(2);
  const root = process.env.OPENTOUR_REPOSITORY_ROOT;
  if (!root || realpathSync(root) !== realpathSync(process.cwd()) || option !== '--result-file' || !destination || extra.length || destination !== process.env.OPENTOUR_RESULT_FILE || target !== process.env.OPENTOUR_TEST_TARGET || !process.env.OPENTOUR_RESULT_NONCE) return 2;
  try { return await executeBrowser({ root, target, destination, nonce: process.env.OPENTOUR_RESULT_NONCE }); } catch { return 2; }
}
if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) void main().then(code => { process.exitCode = code; });
