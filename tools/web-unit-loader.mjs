import { existsSync, readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { extname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { transformSync } from 'esbuild';

const repository = pathToFileURL(`${process.env.OPENTOUR_REPOSITORY_ROOT}/`).href;
const local = url => url?.startsWith(repository) && !url.includes('/node_modules/');

registerHooks({
  resolve(specifier, context, nextResolve) {
    try {
      return nextResolve(specifier, context);
    } catch (error) {
      const directoryImport = error.code === 'ERR_UNSUPPORTED_DIR_IMPORT';
      if ((!directoryImport && error.code !== 'ERR_MODULE_NOT_FOUND') || !local(context.parentURL) ||
          !(specifier.startsWith('./') || specifier.startsWith('../'))) throw error;
      const url = new URL(specifier, context.parentURL);
      if (!local(url.href)) throw error;
      const extension = extname(url.pathname);
      if (extension === '.js') url.pathname = url.pathname.slice(0, -extension.length);
      const stem = directoryImport ? url.pathname.replace(/\/$/, '') + '/index' : url.pathname;
      for (const sourceExtension of ['.ts', '.tsx']) {
        const candidate = new URL(url);
        candidate.pathname = stem + sourceExtension;
        if (existsSync(candidate)) return nextResolve(candidate.href, context);
      }
      throw error;
    }
  },
  load(url, context, nextLoad) {
    if (!local(url)) return nextLoad(url, context);
    const file = fileURLToPath(url);
    const extension = extname(file);
    if (extension === '.css') {
      // Unit tests render markup. Browser suites load and verify the actual stylesheets.
      return { format: 'module', shortCircuit: true, source: 'export {};' };
    }
    if (extension === '.svg') {
      const assetPath = '/' + url.slice(repository.length);
      return { format: 'module', shortCircuit: true, source: `export default ${JSON.stringify(assetPath)};` };
    }
    if (extension === '.json' && !context.importAttributes.type) {
      const data = readFileSync(file, 'utf8');
      return { format: 'module', shortCircuit: true, source: `export default JSON.parse(${JSON.stringify(data)});` };
    }
    if (extension !== '.ts' && extension !== '.tsx') return nextLoad(url, context);
    const source = transformSync(readFileSync(file, 'utf8'), {
      sourcefile: file, loader: extension.slice(1), format: 'esm', target: 'node24',
      jsx: 'automatic', sourcemap: 'inline',
    }).code;
    return { format: 'module', shortCircuit: true, source };
  },
});
