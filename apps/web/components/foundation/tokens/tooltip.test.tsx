import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { placeTooltip, Tooltip } from './tooltip';

describe('OPE-313 supplemental tooltip', () => {
  it('preserves the trigger name and existing description while giving each tooltip a distinct ID', () => {
    const html = renderToStaticMarkup(<><Tooltip description="View exact values and source details.">
      <button aria-label="Exact values" aria-describedby="persistent-source">Details</button>
    </Tooltip><Tooltip description="A separate description."><button>Other details</button></Tooltip></>);
    const ids = [...html.matchAll(/id="([^"]+)" role="tooltip"/g)].map(match => match[1]);
    expect(ids).toHaveLength(2);
    expect(new Set(ids).size).toBe(2);
    expect(html).toContain(`aria-describedby="persistent-source ${ids[0]}"`);
    expect(html).toContain('aria-label="Exact values"');
    expect(html).not.toContain('aria-labelledby');
    expect(html.match(/ hidden=""/g)).toHaveLength(2);
  });
  it('escapes supplied text and keeps supplemental content noninteractive', () => {
    const html = renderToStaticMarkup(<Tooltip description={['<button>Not an action</button>', 'Source details.']} appearance="evidence"><button>Evidence</button></Tooltip>);
    expect(html).toContain('&lt;button&gt;Not an action&lt;/button&gt;');
    expect(html.match(/<button/g)).toHaveLength(1);
    expect(html).not.toMatch(/role="(dialog|alert|status)"|tabindex=/);
  });
  it.each(['0.00993608194445772', '-0.12345678', '+2.0000001'])('retains the exact long fraction %s without adding separators or rounding', value => {
    const html = renderToStaticMarkup(<Tooltip description={`Exact value: ${value}`}><button>Details</button></Tooltip>);
    expect(html).toContain('class="ot-tooltip-fraction"');
    expect(html.replace(/<[^>]+>/g, '')).toBe(`DetailsExact value: ${value}`);
    expect(html).not.toContain('<wbr');
  });
  it.each(['', '   ', []])('rejects empty supplemental content %j', description => {
    expect(() => renderToStaticMarkup(<Tooltip description={description}><button>Details</button></Tooltip>)).toThrow();
  });
  it('refuses an undeclared appearance or a component that would swallow trigger props', () => {
    expect(() => renderToStaticMarkup(<Tooltip description="Details" appearance={'other' as 'help'}><button>Details</button></Tooltip>)).toThrow();
    const Wrapper = () => <button>Details</button>;
    expect(() => renderToStaticMarkup(<Tooltip description="Details"><Wrapper /></Tooltip>)).toThrow();
  });
  it('places below at the native 16px gap and preserves the 320px margins', () => {
    expect(placeTooltip({ left: 16, right: 304, top: 394, bottom: 438 }, 288, 88, { width: 320, height: 800 }))
      .toEqual({ left: 16, top: 454, maxHeight: 330, side: 'bottom' });
  });
  it('flips only for insufficient bottom space and clamps horizontal edges including RTL', () => {
    const anchor = { left: 270, right: 314, top: 700, bottom: 744 };
    expect(placeTooltip(anchor, 288, 100, { width: 320, height: 800 })).toEqual({ left: 16, top: 584, maxHeight: 668, side: 'top' });
    expect(placeTooltip({ left: 100, right: 400, top: 50, bottom: 94 }, 288, 42, { width: 768, height: 1024 }, true).left).toBe(112);
  });
  it('constrains long supplemental text to the larger available side without covering the trigger', () => {
    expect(placeTooltip({ left: 16, right: 100, top: 200, bottom: 244 }, 288, 600, { width: 320, height: 400 }))
      .toEqual({ left: 16, top: 16, maxHeight: 168, side: 'top' });
  });
});
