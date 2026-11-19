import { build, type BuildOptions, type Plugin } from 'esbuild';
import { readFile } from 'node:fs/promises';
import { basename, dirname, extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const browserBuild = {
  bundle: true,
  write: false,
  platform: 'browser',
  target: 'es2023',
  jsx: 'automatic',
  minify: true,
  logLevel: 'silent',
  define: { 'process.env.NODE_ENV': '"production"' },
} as const satisfies BuildOptions;

const inlineMedia = { '.woff2': 'dataurl', '.svg': 'dataurl' } as const;

// The offline documents inject selected styles as strings after their gallery script loads.
const inlineStyles: Plugin = {
  name: 'presentation-inline-styles',
  setup(plugin) {
    plugin.onResolve({ filter: /^\.{1,2}\/.*\.css\?inline$/ }, args => ({
      path: resolve(args.resolveDir, args.path.slice(0, -'?inline'.length)),
      namespace: 'presentation-inline-styles',
    }));
    plugin.onLoad({ filter: /\.css$/, namespace: 'presentation-inline-styles' }, async ({ path }) => {
      const result = await build({
        ...browserBuild,
        entryPoints: [path],
        loader: inlineMedia,
      });
      const stylesheet = result.outputFiles[0];
      if (!stylesheet) throw new Error(`Missing inline fixture stylesheet ${path}`);
      return { contents: stylesheet.text, loader: 'text' };
    });
  },
};

const tokens = fileURLToPath(new URL('../../../components/foundation/tokens/', import.meta.url));
const indicators = ['loading-light.svg', 'loading-dark.svg'];

// The component's two module-relative SVGs are part of the offline fixture, including classic scripts.
const inlineIndicators: Plugin = {
  name: 'presentation-inline-indicators',
  setup(plugin) {
    plugin.onLoad({ filter: /\/tokens\/index\.tsx$/ }, async ({ path }) => {
      if (path !== join(tokens, 'index.tsx')) return;
      let contents = await readFile(path, 'utf8');
      for (const name of indicators) {
        const expression = `new URL('./assets/${name}', import.meta.url).href`;
        if (!contents.includes(expression)) throw new Error(`Missing module-relative fixture asset ${name}`);
        const bytes = await readFile(join(tokens, 'assets', name));
        contents = contents.replace(expression, JSON.stringify(`data:image/svg+xml;base64,${bytes.toString('base64')}`));
      }
      return { contents, loader: 'tsx', resolveDir: dirname(path) };
    });
  },
};

export async function buildInlineFixture(
  entry: string,
  format: 'esm' | 'iife',
  source?: string,
  { requireStylesheet = true }: { requireStylesheet?: boolean } = {},
) {
  const directory = dirname(entry);
  const result = await build({
    ...browserBuild,
    absWorkingDir: directory,
    ...(source === undefined ? { entryPoints: [entry] } : {
      stdin: { contents: source, sourcefile: basename(entry), resolveDir: directory, loader: 'tsx' },
    }),
    outdir: join(directory, '.fixture-output'),
    loader: inlineMedia,
    format,
    plugins: [inlineStyles, inlineIndicators],
  });
  const script = result.outputFiles.find(file => extname(file.path) === '.js');
  const stylesheet = result.outputFiles.find(file => extname(file.path) === '.css');
  if (!script) throw new Error(`Missing fixture script for ${entry}`);
  if (requireStylesheet && !stylesheet) throw new Error(`Missing fixture stylesheet for ${entry}`);
  return { script: script.text, css: stylesheet?.text ?? '' };
}

export function buildHostedFixture(entry: string, outdir: string) {
  return build({
    ...browserBuild,
    absWorkingDir: dirname(entry),
    entryPoints: { index: entry },
    outdir,
    format: 'esm',
    entryNames: 'assets/[name]-[hash]',
    assetNames: 'assets/[name]-[hash]',
    loader: { '.woff2': 'file', '.svg': 'file' },
  });
}
