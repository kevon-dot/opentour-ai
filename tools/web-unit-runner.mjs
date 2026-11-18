import { writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { run } from 'node:test';
import { fileURLToPath } from 'node:url';

const { OPENTOUR_REPOSITORY_ROOT: root, OPENTOUR_TEST_TARGET: target,
  OPENTOUR_RESULT_NONCE: nonce, OPENTOUR_RESULT_FILE: destination } = process.env;
const selection = resolve(root, target);
const loader = fileURLToPath(new URL('./web-unit-loader.mjs', import.meta.url));
const tests = [];
let failedSuite = false;
let skippedSuite = false;
let failedCollection = false;
let emptyFile = false;
let focused = false;
let summary;

for await (const { type, data } of run({
  files: [selection], isolation: 'process', concurrency: 1, only: false,
  execArgv: ['--enable-source-maps', '--experimental-test-module-mocks', '--import', loader],
})) {
  if (type === 'test:summary' && !data.file) summary = data;
  if (type === 'test:diagnostic' &&
      data.message === "'only' and 'runOnly' require the --test-only command-line option.") focused = true;
  if (type !== 'test:pass' && type !== 'test:fail') continue;
  if (data.entryFile === undefined && data.file === selection && data.name === selection) {
    // Node emits a file outcome when the worker registers no actual tests.
    failedCollection = type === 'test:fail';
    emptyFile = type === 'test:pass';
    continue;
  }
  if (data.entryFile !== selection) throw new Error('Unexpected Node test entry file');
  if (data.details.type === 'suite') {
    skippedSuite ||= Boolean(data.skip || data.todo);
    failedSuite ||= type === 'test:fail';
    continue;
  }
  const status = data.skip || data.todo ? 'skipped' : type === 'test:pass' ? 'passed' : 'failed';
  tests.push({ id: `${target}::${tests.length}`, file: target, status, mode: null });
}

const passed = tests.filter(test => test.status === 'passed').length;
const failed = tests.filter(test => test.status === 'failed').length;
const skipped = tests.length - passed - failed;
const consistent = summary && summary.counts.tests === tests.length + Number(emptyFile) &&
  summary.counts.passed === passed + Number(emptyFile) && summary.counts.failed + summary.counts.cancelled === failed &&
  summary.counts.skipped + summary.counts.todo === skipped;
let reason = null;
if (focused || failedCollection) reason = 'INVALID_CONFIGURATION';
else if (skipped || skippedSuite) reason = 'REQUIRED_TEST_SKIPPED';
else if (!consistent) reason = 'RUNNER_ERROR';
else if (failed) reason = 'ASSERTION_FAILED';
else if (failedSuite || !summary.success) reason = 'RUNNER_ERROR';
else if (!tests.length) reason = 'NO_TESTS';
writeFileSync(destination, JSON.stringify({
  schema_version: 'opentour.test-result.v1', runner: 'node_test', target, binding_nonce: nonce,
  kind: 'simulated', mode: null, collected: tests.length, passed, failed, skipped, tests,
  status: reason === null ? 'PASS' : 'FAIL', reason_code: reason, cleanup_complete: true,
}), { flag: 'wx', mode: 0o600 });
process.exitCode = reason === null ? 0 : ['NO_TESTS', 'INVALID_CONFIGURATION'].includes(reason) ? 2 : 1;
