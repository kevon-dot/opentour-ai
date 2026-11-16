import { build } from 'vite';
import { createServer } from 'node:http';
import type { Socket } from 'node:net';
import { lstat, realpath, mkdtemp, rm } from 'node:fs/promises';
import { dirname, extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const contentTypes: Record<string, string> = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.woff2': 'font/woff2',
};

/** Test-only static host. It exposes no product API, proxy, credentials, or persistent state. */
export async function startFoundationServer(runId: string, outputDirectory: string): Promise<{
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
    const result = await build({
      root: dirname(fileURLToPath(import.meta.url)), configFile: false, envFile: false,
      publicDir: false, cacheDir: join(owned, 'cache'), logLevel: 'silent',
      build: { write: false, outDir: join(owned, 'output'), emptyOutDir: false,
        assetsInlineLimit: 0, sourcemap: false, minify: true, target: 'es2023' },
      // JSX transformation is automatic; no React development server or HMR runtime is used.
      oxc: { jsx: { runtime: 'automatic' } },
    });
    const bundles = Array.isArray(result) ? result : [result];
    const assets = new Map<string, { bytes: Buffer; type: string }>();
    let bytes = 0;
    for (const bundle of bundles) {
      if (!('output' in bundle)) { await bundle.close(); throw new Error('Unexpected build watcher'); }
      for (const item of bundle.output) {
        if (item.fileName.split('/').some(part => !part || part === '.' || part === '..') ||
          item.fileName.includes('\\') || assets.has(`/${item.fileName}`)) throw new Error('Invalid build asset');
        const payload = item.type === 'chunk' ? Buffer.from(item.code) : Buffer.from(item.source);
        bytes += payload.length;
        const type = contentTypes[extname(item.fileName)];
        if (!type || bytes > 4 * 1024 * 1024) throw new Error('Unexpected build asset');
        assets.set(`/${item.fileName}`, { bytes: payload, type });
      }
    }
    if (!assets.has('/index.html')) throw new Error('Missing compiled gallery');
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
      const asset = assets.get(path === '/' ? '/index.html' : path ?? '');
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
