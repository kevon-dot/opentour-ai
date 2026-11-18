import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { readFileSync, mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { comparePixels, decodePng, parsePolicy, type Raster } from './visual-comparison.ts';
import { collectVisualComparisons } from '../../../../../tools/browser-test-runner.ts';

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
      assert.equal(createHash('sha256').update(bytes).digest('hex'), binding.baseline_sha256);
      assert.partialDeepStrictEqual(comparePixels(baseline, baseline, policy.vector), { variant: 'baseline', changed_pixels: 0 });
      assert.deepEqual(comparePixels(baseline, alternate(baseline), policy.vector), { variant: 'reviewed_corner_vector', changed_pixels: 14, bounds: { min_x: 290, min_y: 627, max_x: 299, max_y: 636 } });
      assert.equal(comparePixels(baseline, alternate(baseline)).variant, null);
    });
    it(`${path}: rejects a subset, unrelated corner change, larger delta and alpha change`, () => {
      const point = policy.vector[0]; const offset = (point.y * baseline.width + point.x) * 4;
      const partial = copy(baseline); partial.data[offset + 1]--;
      const unrelated = alternate(baseline); unrelated.data[(630 * baseline.width + 295) * 4]--;
      const larger = alternate(baseline); larger.data[offset + 1]--;
      const alpha = alternate(baseline); alpha.data[offset + 3]--;
      for (const image of [partial, unrelated, larger, alpha]) assert.equal(comparePixels(baseline, image, policy.vector).variant, null);
    });
    it(`${path}: rejects any outside change and mismatched image dimensions`, () => {
      const changed = alternate(baseline); changed.data[0]--;
      assert.equal(comparePixels(baseline, changed, policy.vector).variant, null);
      assert.equal(comparePixels(baseline, { ...baseline, width: 321 }, policy.vector).variant, null);
      assert.equal(comparePixels(baseline, { ...baseline, data: baseline.data.slice(4) }, policy.vector).variant, null);
    });
  }
  it('keeps every other approved image exact', () => {
    const review = JSON.parse(readFileSync(`${directory}baseline-review.json`, 'utf8'));
    assert.equal(createHash('sha256').update(policyBytes).digest('hex'), review.reviewed_alternate_policy.sha256);
    const strictPaths = Object.keys(review.images).filter(path => !(path in policy.images));
    assert.equal((strictPaths)?.length, 14);
    for (const path of strictPaths) {
      const expected = decodePng(readFileSync(`${directory}${path}`));
      const changed = copy(expected); changed.data[0]--;
      assert.equal(comparePixels(expected, changed).variant, null);
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
      assert.throws(() => parsePolicy(Buffer.from(JSON.stringify(value))));
    }
  });
  it('binds retained numeric results to the exact raw PNG and rejects tampering or missing files', () => {
    const root = mkdtempSync(join(tmpdir(), 'opentour-visual-evidence-'));
    try {
      mkdirSync(join(root, 'artifacts'));
      const png = readFileSync(`${directory}${Object.keys(policy.images)[0]}`);
      const pngHash = createHash('sha256').update(png).digest('hex');
      const path = 'artifacts/billing320-adverse-text.json'; const pngPath = path.replace('.json', '.png');
      const value = { mode: 'customer_controlled', expected_rejection: true, actual_sha256: pngHash, variant: null, changed_pixels: 1, bounds: { min_x: 0, min_y: 0, max_x: 0, max_y: 0 } };
      const bytes = Buffer.from(JSON.stringify(value)); writeFileSync(join(root, path), bytes); writeFileSync(join(root, pngPath), png);
      const output = { path, sha256: createHash('sha256').update(bytes).digest('hex') }; const image = { path: pngPath, sha256: pngHash };
      assert.deepEqual(collectVisualComparisons(root, [output, image])[0].comparison, value);
      assert.throws(() => collectVisualComparisons(root, [output]));
      assert.throws(() => collectVisualComparisons(root, [output, output, image]));
      assert.throws(() => collectVisualComparisons(root, [output, { ...image, sha256: '0'.repeat(64) }]));
      writeFileSync(join(root, path), JSON.stringify({ ...value, changed_pixels: 0 }));
      assert.throws(() => collectVisualComparisons(root, [output, image]));
      const changed = readFileSync(join(root, path));
      assert.throws(() => collectVisualComparisons(root, [{ path, sha256: createHash('sha256').update(changed).digest('hex') }, image]));
      const coercedMode = Buffer.from(JSON.stringify({ ...value, mode: ['customer_controlled'] }));
      writeFileSync(join(root, path), coercedMode);
      assert.throws(() => collectVisualComparisons(root, [{ path, sha256: createHash('sha256').update(coercedMode).digest('hex') }, image]));
    } finally { rmSync(root, { recursive: true, force: true }); }
  });
  it('binds retained positive variants to the reviewed policy and correct changed-pixel count', () => {
    const root = mkdtempSync(join(tmpdir(), 'opentour-visual-evidence-'));
    try {
      mkdirSync(join(root, 'artifacts')); mkdirSync(join(root, 'apps/web/tests/fixtures/harness'), { recursive: true });
      writeFileSync(join(root, 'apps/web/tests/fixtures/harness/visual-comparison-policy.json'), policyBytes);
      const binding = policy.images['baselines/darwin-customer-controlled/billing-320.png'];
      const path = 'artifacts/billing320-comparison.json'; const pngPath = path.replace('.json', '.png');
      const png = readFileSync(`${directory}baselines/darwin-customer-controlled/billing-320.png`); writeFileSync(join(root, pngPath), png);
      const value = { mode: 'customer_controlled', policy_sha256: createHash('sha256').update(policyBytes).digest('hex'), baseline_sha256: binding.baseline_sha256, actual_sha256: binding.baseline_sha256, variant: 'baseline', changed_pixels: 0, bounds: null };
      function collect(candidate: typeof value) {
        const bytes = Buffer.from(JSON.stringify(candidate)); writeFileSync(join(root, path), bytes);
        return collectVisualComparisons(root, [{ path, sha256: createHash('sha256').update(bytes).digest('hex') }, { path: pngPath, sha256: binding.baseline_sha256 }]);
      }
      assert.equal(collect(value)[0].comparison.variant, 'baseline');
      assert.throws(() => collect({ ...value, policy_sha256: '0'.repeat(64) }));
      assert.throws(() => collect({ ...value, variant: 'reviewed_corner_vector' }));
    } finally { rmSync(root, { recursive: true, force: true }); }
  });
});
