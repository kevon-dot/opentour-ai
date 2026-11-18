import { renderToStaticMarkup } from 'react-dom/server';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import { placeTooltip, Tooltip } from './tooltip';

describe('OPE-313 supplemental tooltip', () => {
  it('preserves the trigger name and existing description while giving each tooltip a distinct ID', () => {
    const html = renderToStaticMarkup(<><Tooltip description="View exact values and source details.">
      <button aria-label="Exact values" aria-describedby="persistent-source">Details</button>
    </Tooltip><Tooltip description="A separate description."><button>Other details</button></Tooltip></>);
    const ids = [...html.matchAll(/id="([^"]+)" role="tooltip"/g)].map(match => match[1]);
    assert.equal((ids)?.length, 2);
    assert.equal(new Set(ids).size, 2);
    assert.ok((html)?.includes(`aria-describedby="persistent-source ${ids[0]}"`));
    assert.ok((html)?.includes('aria-label="Exact values"'));
    assert.ok((html)?.includes('aria-labelledby') === false);
    assert.equal((html.match(/ hidden=""/g))?.length, 2);
  });
  it('escapes supplied text and keeps supplemental content noninteractive', () => {
    const html = renderToStaticMarkup(<Tooltip description={['<button>Not an action</button>', 'Source details.']} appearance="evidence"><button>Evidence</button></Tooltip>);
    assert.ok((html)?.includes('&lt;button&gt;Not an action&lt;/button&gt;'));
    assert.equal((html.match(/<button/g))?.length, 1);
    assert.doesNotMatch(html, /role="(dialog|alert|status)"|tabindex=/);
  });
  testCases(['0.00993608194445772', '-0.12345678', '+2.0000001'])('retains the exact long fraction %s without adding separators or rounding', value => {
    const html = renderToStaticMarkup(<Tooltip description={`Exact value: ${value}`}><button>Details</button></Tooltip>);
    assert.ok((html)?.includes('class="ot-tooltip-fraction"'));
    assert.equal(html.replace(/<[^>]+>/g, ''), `DetailsExact value: ${value}`);
    assert.ok((html)?.includes('<wbr') === false);
  });
  testCases(['', '   ', []])('rejects empty supplemental content %j', (...args) => {
    assert.throws(() => renderToStaticMarkup(<Tooltip description={args[0] as string}><button>Details</button></Tooltip>));
  });
  it('refuses an undeclared appearance or a component that would swallow trigger props', () => {
    assert.throws(() => renderToStaticMarkup(<Tooltip description="Details" appearance={'other' as 'help'}><button>Details</button></Tooltip>));
    const Wrapper = () => <button>Details</button>;
    assert.throws(() => renderToStaticMarkup(<Tooltip description="Details"><Wrapper /></Tooltip>));
  });
  it('places below at the native 16px gap and preserves the 320px margins', () => {
    assert.deepEqual(placeTooltip({ left: 16, right: 304, top: 394, bottom: 438 }, 288, 88, { width: 320, height: 800 }), { left: 16, top: 454, maxHeight: 330, side: 'bottom' });
  });
  it('flips only for insufficient bottom space and clamps horizontal edges including RTL', () => {
    const anchor = { left: 270, right: 314, top: 700, bottom: 744 };
    assert.deepEqual(placeTooltip(anchor, 288, 100, { width: 320, height: 800 }), { left: 16, top: 584, maxHeight: 668, side: 'top' });
    assert.equal(placeTooltip({ left: 100, right: 400, top: 50, bottom: 94 }, 288, 42, { width: 768, height: 1024 }, true).left, 112);
  });
  it('constrains long supplemental text to the larger available side without covering the trigger', () => {
    assert.deepEqual(placeTooltip({ left: 16, right: 100, top: 200, bottom: 244 }, 288, 600, { width: 320, height: 400 }), { left: 16, top: 16, maxHeight: 168, side: 'top' });
  });
});
