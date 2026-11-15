import { executeBrowser, type Probe } from '../../../../../tools/browser-test-runner.ts';
const [root, destination, nonce, probe] = process.argv.slice(2);
const target = probe === 'directory' ? 'apps/web/tests/fixtures/harness/probes/directory' : probe === 'zero' ? 'apps/web/tests/fixtures/harness/probes/zero.spec.ts' : 'apps/web/tests/fixtures/harness/probes/probe with spaces.spec.ts';
process.exitCode = await executeBrowser({ root, destination, nonce, target, probe: probe as Probe, timeoutMs: probe === 'timeout' ? 3000 : 30_000 });
