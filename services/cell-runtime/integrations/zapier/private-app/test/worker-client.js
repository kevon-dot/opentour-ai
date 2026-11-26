'use strict';

// Local generated conformance only. Credentials arrive on stdin and are never printed.
const fs = require('node:fs');
const core = require('zapier-platform-core');
const app = require('../index');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
async function main() {
  const action = app.creates[input.operation];
  if (!action) throw new Error('unknown_test_operation');
  try {
    const result = await action.operation.perform({ errors: core.errors }, input.bundle);
    process.stdout.write(JSON.stringify({ ok: true, result }) + '\n');
  } catch (error) {
    let code = 'unexpected_error';
    try { code = JSON.parse(error.message).code; } catch { /* Never print the error. */ }
    process.stdout.write(JSON.stringify({ ok: false, code }) + '\n');
  }
}
main().catch(() => { process.stderr.write('worker_client_failed\n'); process.exitCode = 1; });
