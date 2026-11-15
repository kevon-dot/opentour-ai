import { writeFileSync, mkdirSync } from 'node:fs';
import { relative, join } from 'node:path';
import type { Reporter, Suite, TestCase, TestResult, FullResult } from '@playwright/test/reporter';
export default class HarnessReporter implements Reporter {
  private tests: TestCase[] = [];
  private errors = 0;
  private collectionErrors: string[] = [];
  private attempts = new Map<string, TestResult[]>();
  onBegin(_config: unknown, suite: Suite) { this.tests = suite.allTests(); }
  onTestEnd(test: TestCase, result: TestResult) { const list = this.attempts.get(test.id) ?? []; list.push(result); this.attempts.set(test.id, list); }
  onError(error: { message?: string }) { this.errors++; this.collectionErrors.push(error.message ?? 'Runner error'); process.stderr.write(`${error.message ?? 'Runner error'}\n`); }
  onEnd(result: FullResult) {
    const root = process.env.OPENTOUR_REPOSITORY_ROOT!;
    const tests = this.tests.map(test => {
      const attempts = this.attempts.get(test.id) ?? [];
      const status = !attempts.length || attempts.every(a => a.status === 'skipped') ? 'skipped' : test.expectedStatus !== 'passed' || attempts.length !== 1 || attempts.some(a => a.status !== 'passed') ? 'failed' : 'passed';
      return { id: `${test.parent.project()?.name}:${test.id}`, file: relative(root, test.location.file).split('\\').join('/'), status, mode: test.parent.project()?.metadata.mode ?? null };
    });
    writeFileSync(process.env.OPENTOUR_BROWSER_RAW!, JSON.stringify({ status: result.status, errors: this.errors, noTests: this.collectionErrors.length > 0 && this.collectionErrors.every(error => error.includes('No tests found')), tests }));
    mkdirSync(process.env.OPENTOUR_BROWSER_OUTPUT!, { recursive: true });
    writeFileSync(join(process.env.OPENTOUR_BROWSER_OUTPUT!, 'failures.json'), JSON.stringify(this.tests.flatMap(test => (this.attempts.get(test.id) ?? []).filter(attempt => attempt.status !== 'passed').map(attempt => ({ id: test.id, errors: attempt.errors.map(error => error.message ?? 'Failure') }))), null, 2));
  }
}
