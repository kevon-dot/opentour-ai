import { buildHostedFixture } from '../../../../tests/fixtures/presentation/build.ts';
import { createServer } from 'node:http';
import type { Socket } from 'node:net';
import { lstat, realpath, mkdtemp, readFile, rm } from 'node:fs/promises';
import { dirname, extname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const contentTypes: Record<string, string> = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.woff2': 'font/woff2',
};

async function buildGalleryAssets(output: string, operationFixture: boolean) {
  const testing = dirname(fileURLToPath(import.meta.url));
  const source = operationFixture ? fileURLToPath(new URL('../../../../tests/fixtures/presentation/', import.meta.url)) : testing;
  const entryFile = operationFixture ? 'operation-state.tsx' : 'gallery.tsx';
  const result = await buildHostedFixture(join(source, entryFile), output);
  const assets = new Map<string, { bytes: Uint8Array; type: string }>();
  let totalBytes = 0;
  function addAsset(path: string, bytes: Uint8Array) {
    const url = `/${path}`;
    if (path.split('/').some(part => !part || part === '.' || part === '..') ||
      path.includes('\\') || assets.has(url)) throw new Error('Invalid build asset');
    const type = contentTypes[extname(path)];
    totalBytes += bytes.length;
    if (!type || totalBytes > 4 * 1024 * 1024) throw new Error('Unexpected build asset');
    assets.set(url, { bytes, type });
  }
  let script = '';
  let stylesheet = '';
  for (const file of result.outputFiles) {
    const path = relative(output, file.path);
    addAsset(path, file.contents);
    if (extname(path) === '.js') script = path;
    if (extname(path) === '.css') stylesheet = path;
  }
  const html = await readFile(join(source, 'index.html'), 'utf8');
  const entry = `<script type="module" src="./${entryFile}"></script>`;
  if (!script || !stylesheet || !html.includes(entry) || !html.includes('</head>')) {
    throw new Error('Missing compiled gallery');
  }
  addAsset('index.html', Buffer.from(html.replace(entry, '').replace('</head>',
    `<script type="module" crossorigin src="/${script}"></script>` +
    `<link rel="stylesheet" crossorigin href="/${stylesheet}"></head>`)));

  // The bundle remains in /assets/ so the component's module-relative URLs resolve below it.
  for (const name of ['loading-light.svg', 'loading-dark.svg']) {
    addAsset(`assets/assets/${name}`, await readFile(join(testing, '..', 'assets', name)));
  }
  return assets;
}

/** Test-only static host. It exposes no product API, proxy, credentials, or persistent state. */
export async function startFoundationServer(runId: string, outputDirectory: string, operationFixture = false): Promise<{
  origin: string; close(): Promise<boolean>;
}> {
  if (!/^[a-f0-9]{32}$/.test(runId) || resolve(outputDirectory) !== outputDirectory ||
    (await lstat(outputDirectory)).isSymbolicLink() || await realpath(outputDirectory) !== outputDirectory) {
    throw new Error('INVALID_CONFIGURATION');
  }
  const owned = await mkdtemp(join(outputDirectory, 'foundation-build-'));
  const sockets = new Set<Socket>();
  const server = createServer();
  let closing: Promise<boolean> | undefined;
  const close = (): Promise<boolean> => closing ??= (async () => {
    let clean = true;
    if (server.listening) {
      const closed = new Promise<boolean>(done => server.close(error => done(!error)));
      for (const socket of sockets) socket.destroy();
      clean = await closed;
    }
    if (server.listening || [...sockets].some(socket => !socket.destroyed)) clean = false;
    try { await rm(owned, { recursive: true, force: false }); } catch { clean = false; }
    return clean;
  })();
  try {
    const assets = await buildGalleryAssets(join(owned, 'output'), operationFixture);
    let origin = '';
    server.requestTimeout = 5000; server.headersTimeout = 5000; server.keepAliveTimeout = 1000;
    server.on('connection', socket => { sockets.add(socket); socket.once('close', () => sockets.delete(socket)); });
    server.on('request', (request, response) => {
      response.setHeader('Cache-Control', 'no-store');
      response.setHeader('X-Content-Type-Options', 'nosniff');
      response.setHeader('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'");
      if (!origin || request.headers.host !== new URL(origin).host ||
        (request.headers.origin !== undefined && request.headers.origin !== origin)) {
        response.writeHead(403).end(); return;
      }
      if (!['GET', 'HEAD'].includes(request.method ?? '')) { response.writeHead(405).end(); return; }
      const path = request.url?.split('?')[0];
      if (path === '/__harness/ready') {
        response.setHeader('Content-Type', 'application/json');
        response.end(request.method === 'HEAD' ? undefined : JSON.stringify({ run_id: runId })); return;
      }
      const asset = assets.get((path === '/' || (operationFixture && path === '/operation-state')) ? '/index.html' : path ?? '');
      if (!asset) { response.writeHead(404).end(); return; }
      response.setHeader('Content-Type', asset.type); response.setHeader('Content-Length', asset.bytes.length);
      response.end(request.method === 'HEAD' ? undefined : asset.bytes);
    });
    await new Promise<void>((ready, reject) => {
      server.once('error', reject);
      server.listen(0, '127.0.0.1', () => { server.off('error', reject); ready(); });
    });
    const address = server.address();
    if (!address || typeof address === 'string') throw new Error('Missing loopback listener');
    origin = `http://127.0.0.1:${address.port}`;
    return { origin, close };
  } catch (error) {
    if (!await close()) throw new Error('Foundation startup cleanup failed', { cause: error });
    throw error;
  }
}
