import { exactSelector, validateTarget } from '../apps/web/tests/fixtures/harness/selection.ts';
import { spawn, execFileSync } from 'node:child_process';
import { existsSync, lstatSync, realpathSync, readFileSync, writeFileSync, mkdirSync, mkdtempSync, rmSync, linkSync, unlinkSync, readdirSync } from 'node:fs';
import { resolve, dirname, relative, join } from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { tmpdir } from 'node:os';
import { createHash, randomBytes } from 'node:crypto';

type Mode = 'customer_controlled' | 'opentour_managed' | null;
type Case = { id: string; file: string; status: 'passed' | 'failed' | 'skipped'; mode: Mode };
type Report = { schema_version: string; runner: string; target: string; binding_nonce: string; kind: string; mode: Mode; collected: number; passed: number; failed: number; skipped: number; tests: Case[]; status: 'PASS' | 'FAIL'; reason_code: string | null; cleanup_complete: boolean };
export type Probe = 'directory' | 'hold' | 'pass' | 'fail' | 'zero' | 'skip' | 'expected_failure' | 'timeout' | 'interrupt' | 'cleanup' | 'network' | 'missing_browser' | 'missing_application';
const harness = 'apps/web/tests/fixtures/harness';
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
  if (!/^[a-f0-9]{32}$/.test(nonce)) throw new Error('INVALID_CONFIGURATION');
  const report: Report = { schema_version: 'opentour.test-result.v1', runner: 'playwright', target, binding_nonce: nonce, kind: 'simulated', mode: null, collected: 0, passed: 0, failed: 0, skipped: 0, tests: [], status: 'FAIL', reason_code: 'INVALID_CONFIGURATION', cleanup_complete: true };
  let code = 2;
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
  const signal = () => { interrupted = true; child?.kill('SIGTERM'); };
  process.on('SIGINT', signal); process.on('SIGTERM', signal);
  try {
    const selection = validateTarget(root, target, !!probe);
    if (probe === 'missing_application' || !existsSync(join(root, harness, 'index.html')) || !existsSync(join(root, harness, 'assets/inter-latin-400-normal.woff2'))) throw new Error('MISSING_APPLICATION');
    process.env.PLAYWRIGHT_BROWSERS_PATH = join(root, '.toolchain/playwright');
    const require = createRequire(join(root, 'package.json'));
    const { chromium } = require('@playwright/test');
    if (probe === 'missing_browser' || !existsSync(chromium.executablePath())) throw new Error('MISSING_BROWSER');
    runDirectory = mkdtempSync(join(tmpdir(), 'opentour-browser-'));
    mkdirSync(join(runDirectory, 'home'));
    artifactDirectory = join(root, 'evidence/OT-BOOT-C06/runs', nonce); mkdirSync(artifactDirectory, { recursive: true });
    const raw = join(runDirectory, 'raw.json');
    const { startFixtureServer } = await import(pathToFileURL(join(root, harness, 'server.ts')).href);
    server = await startFixtureServer(nonce, probe === 'cleanup');
    const ready = await fetch(`${server!.origin}/__harness/ready`, { signal: AbortSignal.timeout(3000) }).then(response => response.json());
    if (ready.run_id !== nonce) throw new Error('MISSING_APPLICATION');
    const environment: NodeJS.ProcessEnv = { PATH: process.env.PATH, HOME: join(runDirectory, 'home'), TMPDIR: runDirectory, CI: 'true', TZ: 'UTC', LANG: 'C.UTF-8', NEXT_TELEMETRY_DISABLED: '1', PLAYWRIGHT_BROWSERS_PATH: join(root, '.toolchain/playwright'), OPENTOUR_REPOSITORY_ROOT: root, OPENTOUR_FIXTURE_ORIGIN: server!.origin, OPENTOUR_BROWSER_RAW: raw, OPENTOUR_BROWSER_OUTPUT: join(artifactDirectory, 'artifacts'), OPENTOUR_BROWSER_PROBE: probe, OPENTOUR_BASELINE_REVIEW_REASON: options.baselineReview?.reason, OPENTOUR_BROWSER_READY_FILE: `${destination}.ready`, OPENTOUR_RESULT_NONCE: nonce, NODE_OPTIONS: `--require=${JSON.stringify(join(root, harness, 'network-node.cjs'))}` };
    const playwright = join(dirname(createRequire(require.resolve('@playwright/test')).resolve('playwright/package.json')), 'cli.js');
    const argv = [playwright, 'test', '--config', join(root, 'playwright.config.ts'), exactSelector(selection.path, selection.directory)];
    if (options.baselineReview) { if (!options.baselineReview.reason || !options.baselineReview.designRevision || probe) throw new Error('INVALID_CONFIGURATION'); argv.push('--update-snapshots=all'); }
    child = spawn(process.execPath, argv, { cwd: root, env: environment, stdio: ['ignore', 'pipe', 'pipe'], detached: false });
    let outputBytes = 0; let log = '';
    const capture = (chunk: Buffer) => { outputBytes += chunk.length; if (outputBytes > 1_048_576) child?.kill('SIGTERM'); else log += chunk.toString(); };
    child.stdout?.on('data', capture); child.stderr?.on('data', capture);
    const track = setInterval(() => { if (!child?.pid) return; try { descendants(child.pid).forEach((stamp, pid) => known.set(pid, stamp)); } catch (error) { processCleanup = false; cleanupDiagnostic = error instanceof Error ? error.message : 'process_inspection'; } }, 200);
    const timeout = setTimeout(() => { timedOut = true; child?.kill('SIGTERM'); }, options.timeoutMs ?? 250_000);
    const interrupt = probe === 'interrupt' ? setTimeout(signal, 1500) : undefined;
    let rawExit: number | null;
    try { rawExit = await new Promise<number | null>((resolveExit, reject) => { child!.once('error', reject); child!.once('close', resolveExit); }); }
    finally { clearInterval(track); clearTimeout(timeout); if (interrupt) clearTimeout(interrupt); }
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
      else report.reason_code = report.failed ? 'ASSERTION_FAILED' : report.skipped ? 'REQUIRED_TEST_SKIPPED' : !report.collected ? 'NO_TESTS' : rawExit || value.status !== 'passed' ? 'RUNNER_ERROR' : null;
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
      let commit: string | null = null; let sourceDirty = true; try { commit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim(); sourceDirty = !!execFileSync('git', ['status', '--porcelain', '--untracked-files=normal'], { cwd: root, encoding: 'utf8' }).trim(); } catch { /* Evidence remains explicitly unbound. */ }
      const evidence = { schema_version: 'opentour.browser-artifacts.v1', issue_key: 'OPE-127', kind: 'simulated', requirement_ids: ['OT-BOOT-C06-AC01', 'OT-BOOT-C06-AC02'], source_refs: ['G1', 'AC-22-01', 'AC-23-01'], product_acceptance: 'NOT_RUN', commit_sha: commit, git_worktree_dirty: sourceDirty, test_source_sha256: sha(lstatSync(join(root, target)).isDirectory() ? JSON.stringify(inventory(root, join(root, target))) : readFileSync(join(root, target))), target, fixture_id: 'simulated-browser-harness', fixture_version: '1', seed: 127, clock: '2026-11-14T12:00:00.000Z', spec_sha256: '96c405228e6e925e3c11ea4d44c0d5cb5b984541bab1a017735e7629ffa72292', cleanup_observation: { processCleanup, applicationCleanup, cleanupDiagnostic }, baseline_review_request: options.baselineReview ?? null, platform: process.platform, architecture: process.arch, playwright_version: JSON.parse(readFileSync(join(root, 'package.json'), 'utf8')).devDependencies['@playwright/test'], viewport_widths: [320, 768, 1280, 1440], locale: 'en-US', timezone: 'UTC', color_scheme: 'light', font_sha256: sha(readFileSync(join(root, harness, 'assets/inter-latin-400-normal.woff2'))), outputs: inventory(root, artifactDirectory), report, limitations: ['Deterministic simulated interaction evidence only.', 'Provider/customer/isolation G1 acceptance NOT_RUN.', 'Canonical Linux visual qualification requires its platform baselines and independent review.'] };
      writeFileSync(join(artifactDirectory, 'evidence.json'), JSON.stringify(evidence, null, 2));
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
