import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { comparePixels, decodePng, parsePolicy, type Raster } from './visual-comparison.ts';

const directory = fileURLToPath(new URL('.', import.meta.url));
const policyBytes = readFileSync(`${directory}visual-comparison-policy.json`);
const policy = parsePolicy(policyBytes);
const copy = (image: Raster): Raster => ({ ...image, data: new Uint8Array(image.data) });
function alternate(image: Raster): Raster {
  const next = copy(image);
  for (const point of policy.vector) for (let channel = 0; channel < 4; channel++) next.data[(point.y * image.width + point.x) * 4 + channel] += point.delta_rgba[channel];
  return next;
}

describe('BH-VISUAL-POLICY: exact reviewed rounded-border alternative', () => {
  for (const [path, binding] of Object.entries(policy.images)) {
    const bytes = readFileSync(`${directory}${path}`);
    const baseline = decodePng(bytes);
    it(`${path}: binds the original image and accepts only the complete alternate`, () => {
      expect(createHash('sha256').update(bytes).digest('hex')).toBe(binding.baseline_sha256);
      expect(comparePixels(baseline, baseline, policy.vector)).toMatchObject({ variant: 'baseline', changed_pixels: 0 });
      expect(comparePixels(baseline, alternate(baseline), policy.vector)).toEqual({ variant: 'reviewed_corner_vector', changed_pixels: 14, bounds: { min_x: 290, min_y: 627, max_x: 299, max_y: 636 } });
      expect(comparePixels(baseline, alternate(baseline)).variant).toBeNull();
    });
    it(`${path}: rejects a subset, unrelated corner change, larger delta and alpha change`, () => {
      const point = policy.vector[0]; const offset = (point.y * baseline.width + point.x) * 4;
      const partial = copy(baseline); partial.data[offset + 1]--;
      const unrelated = alternate(baseline); unrelated.data[(630 * baseline.width + 295) * 4]--;
      const larger = alternate(baseline); larger.data[offset + 1]--;
      const alpha = alternate(baseline); alpha.data[offset + 3]--;
      for (const image of [partial, unrelated, larger, alpha]) expect(comparePixels(baseline, image, policy.vector).variant).toBeNull();
    });
    it(`${path}: rejects any outside change and mismatched image dimensions`, () => {
      const changed = alternate(baseline); changed.data[0]--;
      expect(comparePixels(baseline, changed, policy.vector).variant).toBeNull();
      expect(comparePixels(baseline, { ...baseline, width: 321 }, policy.vector).variant).toBeNull();
      expect(comparePixels(baseline, { ...baseline, data: baseline.data.slice(4) }, policy.vector).variant).toBeNull();
    });
  }
  it('keeps every other approved image exact', () => {
    const review = JSON.parse(readFileSync(`${directory}baseline-review.json`, 'utf8'));
    expect(createHash('sha256').update(policyBytes).digest('hex')).toBe(review.reviewed_alternate_policy.sha256);
    const strictPaths = Object.keys(review.images).filter(path => !(path in policy.images));
    expect(strictPaths).toHaveLength(14);
    for (const path of strictPaths) {
      const expected = decodePng(readFileSync(`${directory}${path}`));
      const changed = copy(expected); changed.data[0]--;
      expect(comparePixels(expected, changed).variant).toBeNull();
    }
  });
  it('rejects masks, scope expansion, duplicate points and widened channel tolerances', () => {
    for (const mutate of [
      (value: typeof policy) => { value.masks.push({}); },
      (value: typeof policy) => { value.images['another.png'] = value.images[Object.keys(value.images)[0]]; },
      (value: typeof policy) => { value.vector[1] = value.vector[0]; },
      (value: typeof policy) => { value.vector[0].delta_rgba[1] = -2; },
      (value: typeof policy) => { value.vector[0].delta_rgba[3] = -1; },
    ]) {
      const value = structuredClone(policy); mutate(value);
      expect(() => parsePolicy(Buffer.from(JSON.stringify(value)))).toThrow();
    }
  });
});
