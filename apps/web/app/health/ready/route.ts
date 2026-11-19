import { bffRuntimeReady } from '../../../lib/server/bff-runtime.js';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(): Promise<Response> {
  return new Response(null, {
    status: await bffRuntimeReady() ? 200 : 503,
    headers: { 'cache-control': 'no-store' },
  });
}
