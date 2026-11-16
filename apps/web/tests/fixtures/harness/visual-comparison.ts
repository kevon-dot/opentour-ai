import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';

export type Raster = { width: number; height: number; data: Uint8Array };
export type PixelDelta = { x: number; y: number; delta_rgba: number[] };
export type Comparison = { variant: 'baseline' | 'reviewed_corner_vector' | null; changed_pixels: number; bounds: { min_x: number; min_y: number; max_x: number; max_y: number } | null };

// Use the PNG decoder shipped by the exactly locked Playwright dependency. No
// decoder fallback, image re-encoding, normalization or baseline write occurs.
const require = createRequire(import.meta.url);
const playwright = createRequire(require.resolve('@playwright/test/package.json'));
const core = createRequire(playwright.resolve('playwright/package.json'));
const { PNG } = require(join(dirname(core.resolve('playwright-core/package.json')), 'lib/utilsBundle.js')) as { PNG: { sync: { read(data: Buffer): Raster } } };
export function decodePng(bytes: Buffer): Raster { return PNG.sync.read(bytes); }

export function comparePixels(expected: Raster, actual: Raster, vector: readonly PixelDelta[] = []): Comparison {
  if (expected.width !== actual.width || expected.height !== actual.height || expected.data.length !== expected.width * expected.height * 4 || actual.data.length !== expected.data.length) {
    return { variant: null, changed_pixels: -1, bounds: null };
  }
  const deltas = new Map<number, readonly number[]>();
  for (const point of vector) {
    const index = point.y * expected.width + point.x;
    if (!Number.isInteger(point.x) || !Number.isInteger(point.y) || point.x < 0 || point.y < 0 || point.x >= expected.width || point.y >= expected.height || deltas.has(index) || point.delta_rgba.length !== 4 || point.delta_rgba[3] !== 0 || !point.delta_rgba.some(value => value === -1) || point.delta_rgba.some(value => value !== 0 && value !== -1)) throw new Error('Invalid reviewed pixel vector');
    deltas.set(index, point.delta_rgba);
  }
  let changed = 0; let alternate = vector.length > 0;
  let minX = expected.width; let minY = expected.height; let maxX = -1; let maxY = -1;
  for (let pixel = 0; pixel < expected.width * expected.height; pixel++) {
    let different = false;
    for (let channel = 0; channel < 4; channel++) {
      const offset = pixel * 4 + channel;
      const delta = actual.data[offset] - expected.data[offset];
      different ||= delta !== 0;
      if (delta !== (deltas.get(pixel)?.[channel] ?? 0)) alternate = false;
    }
    if (different) {
      changed++;
      const x = pixel % expected.width; const y = Math.floor(pixel / expected.width);
      minX = Math.min(minX, x); minY = Math.min(minY, y); maxX = Math.max(maxX, x); maxY = Math.max(maxY, y);
    }
  }
  return { variant: changed === 0 ? 'baseline' : alternate ? 'reviewed_corner_vector' : null, changed_pixels: changed, bounds: changed ? { min_x: minX, min_y: minY, max_x: maxX, max_y: maxY } : null };
}

export type VisualPolicy = {
  schema_version: string; review_status: string; reviewer: string; review_scope: string; design_revision: string; reason: string;
  platform: string; architecture: string; playwright: string; chromium_build: string;
  viewport: { width: number; height: number }; component: string; default_comparison: string; alternate_comparison: string; masks: unknown[];
  images: Record<string, { baseline_sha256: string; observed_alternate_sha256: string }>; vector: PixelDelta[];
};
export function parsePolicy(bytes: Buffer): VisualPolicy {
  const value = JSON.parse(bytes.toString('utf8')) as VisualPolicy;
  const names = ['baselines/darwin-customer-controlled/billing-320.png', 'baselines/darwin-opentour-managed/billing-320.png'];
  if (value.schema_version !== 'opentour.harness-visual-policy.v1' || value.review_status !== 'APPROVED' || value.design_revision !== 'OPE-127-fixture-v1' || value.platform !== 'darwin' || value.architecture !== 'arm64' || value.playwright !== '1.65.1' || value.chromium_build !== '1255' || value.viewport.width !== 320 || value.viewport.height !== 1100 || value.component !== 'section[aria-labelledby="view-title"]' || value.default_comparison !== 'exact_rgba' || value.alternate_comparison !== 'exact_complete_delta_vector' || value.masks.length !== 0 || Object.keys(value.images).length !== 2 || names.some(name => !value.images[name] || !/^[a-f0-9]{64}$/.test(value.images[name].baseline_sha256) || !/^[a-f0-9]{64}$/.test(value.images[name].observed_alternate_sha256)) || value.vector.length !== 14 || value.vector.some(point => point.x < 290 || point.x > 299 || point.y < 627 || point.y > 636)) throw new Error('Unreviewed visual policy');
  // Validate coordinates/channels even when a run happens to match the baseline.
  const empty = { width: 320, height: 1100, data: new Uint8Array(320 * 1100 * 4) };
  comparePixels(empty, empty, value.vector);
  return value;
}
