// Explicit authoring operation. Ordinary wrapper runs cannot select this path or enable updates.
// Generated candidates require independent visual review and a recorded source/design revision.
import { mkdtempSync, rmSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';
import { executeBrowser } from '../../../../../tools/browser-test-runner.ts';
const [reason, designRevision, ...extra] = process.argv.slice(2);
if (!reason || !designRevision || extra.length) throw new Error('Provide a review reason and design revision.');
const directory = realpathSync(mkdtempSync(join(tmpdir(), 'opentour-baseline-review-')));
try {
  process.exitCode = await executeBrowser({ root: process.cwd(), target: 'apps/web/tests/browser-harness.browser.spec.ts', destination: join(directory,'result.json'), nonce: randomBytes(16).toString('hex'), baselineReview: { reason, designRevision } });
} finally { rmSync(directory,{recursive:true,force:true}); }
