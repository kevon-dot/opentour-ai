import { existsSync, lstatSync, realpathSync, readFileSync, readdirSync } from 'node:fs';
import { resolve, join, sep } from 'node:path';
const harness = 'apps/web/tests/fixtures/harness';
export function exactSelector(path: string, directory: boolean) { return `^${path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}${directory ? `${sep === '\\' ? '\\\\' : '/'}.*` : ''}$`; }
export function validateTarget(root: string, target: string, internal = false) {
  if (!target || target.startsWith('-') || target.startsWith('/') || target.includes('\\') || target.split('/').some(p => !p || p === '.' || p === '..') || /[^A-Za-z0-9_ ./-]/.test(target)) throw new Error('INVALID_CONFIGURATION');
  const path = resolve(root, target);
  let cursor = root;
  for (const part of target.split('/')) { cursor = join(cursor, part); if (!existsSync(cursor) || lstatSync(cursor).isSymbolicLink()) throw new Error('INVALID_CONFIGURATION'); }
  if (!realpathSync(path).startsWith(`${realpathSync(root)}${sep}`)) throw new Error('INVALID_CONFIGURATION');
  const directory = lstatSync(path).isDirectory();
  if (!directory && !target.endsWith('.spec.ts')) throw new Error('INVALID_CONFIGURATION');
  if (internal) { if (!target.startsWith(`${harness}/probes/`)) throw new Error('INVALID_CONFIGURATION'); }
  else { const manifest = JSON.parse(readFileSync(join(root, 'packages/contracts/common/command-manifest.json'), 'utf8')); if (!manifest.browser_suites.includes(target) || manifest.browser_profiles[target]?.kind !== 'simulated') throw new Error('INVALID_CONFIGURATION'); }
  if (directory) {
    const inspect = (folder: string) => { for (const entry of readdirSync(folder, { withFileTypes: true })) { if (entry.isSymbolicLink()) throw new Error('INVALID_CONFIGURATION'); if (entry.isDirectory()) inspect(join(folder, entry.name)); } };
    inspect(path);
  }
  return { path, directory };
}
