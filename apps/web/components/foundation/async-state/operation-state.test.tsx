import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
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
    expect(output).toHaveLength(3);
    for (const [i, key] of (['operation', 'decision', 'runtime'] as const).entries()) {
      expect(output[i]).toContain('aria-label="' + ['Operation', 'Decision', 'Runtime'][i] + '"');
      for (const value of [supplied[key].status, supplied[key].resource, supplied[key].observedAt]) {
        expect(output[i]).toContain(value);
      }
    }
    expect(output.join('')).not.toMatch(/completed|Incumbent active|Candidate active/);
  });
  it('changes only the operation when a supplied completion arrives', () => {
    const before = regions(supplied);
    const after = regions({ ...supplied, operation: { ...supplied.operation, kind: 'progress', state: 'complete',
      status: 'Evaluation completed', detail: 'Refund status' } });
    expect(after[0]).toContain('Evaluation completed');
    expect(after.slice(1)).toEqual(before.slice(1));
  });
  it('preserves the active incumbent when the candidate is held', () => {
    const fixture = { ...supplied, runtime: { kind: 'badge', tone: 'success', resource: 'Incumbent r0',
      status: 'Incumbent active', detail: '0% candidate traffic', observedAt: '09:42 UTC' } } satisfies OperationStateProps;
    const after = regions({ ...fixture, decision: { ...fixture.decision, status: 'Candidate on hold', tone: 'hold' } });
    expect(after[1]).toContain('Candidate on hold');
    expect(after[2]).toEqual(regions(fixture)[2]);
    expect(after[2]).not.toContain('Candidate r1');
  });
  it.each(['operation', 'decision', 'runtime'] as const)('requires visible data in the %s region', key => {
    for (const field of ['resource', 'status', 'observedAt']) {
      expect(() => regions({ ...supplied, [key]: { ...supplied[key], [field]: ' ' } })).toThrow('visible');
    }
  });
  it('shows caller-supplied escaped values, including an unknown observation time', () => {
    const runtime = { ...supplied.runtime, resource: '<script>sentinel</script>', observedAt: 'Unknown' };
    const html = regions({ ...supplied, runtime })[2];
    expect(html).toContain('&lt;script&gt;sentinel&lt;/script&gt;');
    expect(html).toContain('Observed Unknown');
  });
});
