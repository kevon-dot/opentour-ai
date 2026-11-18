import { renderToStaticMarkup } from 'react-dom/server';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import { OperationState, type OperationStateProps } from './operation-state';

const supplied = {
  operation: { kind: 'notice', tone: 'info', resource: 'Promotion r1', status: 'Promotion requested',
    detail: 'Promotion intent was accepted.', observedAt: '09:40 UTC' },
  decision: { kind: 'notice', tone: 'success', resource: 'Candidate r1', status: 'Pass',
    detail: 'Current policy permits the exact candidate and scope.', observedAt: '09:41 UTC' },
  runtime: { kind: 'data', state: 'unknown-outcome', resource: 'refund-status/v2', status: 'Runtime unknown',
    detail: 'The serving identity is unconfirmed.', observedAt: '09:42 UTC' },
} satisfies OperationStateProps;

function regions(props: OperationStateProps) {
  return Array.from(renderToStaticMarkup(<OperationState {...props} />).matchAll(/<section\b[^>]*>[\s\S]*?<\/section>/g), m => m[0]);
}

describe('independent supplied observations', () => {
  it('retains each resource, status and time in its own named region', () => {
    const output = regions(supplied);
    assert.equal((output)?.length, 3);
    for (const [i, key] of (['operation', 'decision', 'runtime'] as const).entries()) {
      assert.ok((output[i])?.includes('aria-label="' + ['Operation', 'Decision', 'Runtime'][i] + '"'));
      for (const value of [supplied[key].status, supplied[key].resource, supplied[key].observedAt]) {
        assert.ok((output[i])?.includes(value));
      }
    }
    assert.doesNotMatch(output.join(''), /completed|Incumbent active|Candidate active/);
  });
  it('changes only the operation when a supplied completion arrives', () => {
    const before = regions(supplied);
    const after = regions({ ...supplied, operation: { ...supplied.operation, kind: 'progress', state: 'complete',
      status: 'Evaluation completed', detail: 'Refund status' } });
    assert.ok((after[0])?.includes('Evaluation completed'));
    assert.deepEqual(after.slice(1), before.slice(1));
  });
  it('preserves the active incumbent when the candidate is held', () => {
    const fixture = { ...supplied, runtime: { kind: 'badge', tone: 'success', resource: 'Incumbent r0',
      status: 'Incumbent active', detail: '0% candidate traffic', observedAt: '09:42 UTC' } } satisfies OperationStateProps;
    const after = regions({ ...fixture, decision: { ...fixture.decision, status: 'Candidate on hold', tone: 'hold' } });
    assert.ok((after[1])?.includes('Candidate on hold'));
    assert.deepEqual(after[2], regions(fixture)[2]);
    assert.ok((after[2])?.includes('Candidate r1') === false);
  });
  testCases(['operation', 'decision', 'runtime'] as const)('requires visible data in the %s region', key => {
    for (const field of ['resource', 'status', 'observedAt']) {
      assert.throws(() => regions({ ...supplied, [key]: { ...supplied[key], [field]: ' ' } }), /visible/);
    }
  });
  it('shows caller-supplied escaped values, including an unknown observation time', () => {
    const runtime = { ...supplied.runtime, resource: '<script>sentinel</script>', observedAt: 'Unknown' };
    const html = regions({ ...supplied, runtime })[2];
    assert.ok((html)?.includes('&lt;script&gt;sentinel&lt;/script&gt;'));
    assert.ok((html)?.includes('Observed Unknown'));
  });
});
