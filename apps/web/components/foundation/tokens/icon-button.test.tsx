import { renderToStaticMarkup } from 'react-dom/server';
import { describe, it, expect } from 'vitest';
import { IconButton } from './icon-button';

describe('IconButton', () => {
  const props = { label: 'Close receipt details', onClick: () => {} };
  it('renders one contextual native button and a decorative icon', () => {
    const html = renderToStaticMarkup(<IconButton {...props} />);
    expect(html).toContain('type="button"');
    expect(html).toContain('aria-label="Close receipt details"');
    expect(html).toContain('aria-hidden="true"');
  });
  it('accepts another decorative asset and contextual description', () => {
    const html = renderToStaticMarkup(<IconButton {...props} iconSrc="/assets/search.svg" describedBy="search-help" />);
    expect(html).toContain('/assets/search.svg');
    expect(html).toContain('aria-describedby="search-help"');
  });
  it.each(['', '  '])('rejects an empty accessible name', label => {
    expect(() => renderToStaticMarkup(<IconButton {...props} label={label} />)).toThrow('accessible name');
  });
  it('uses the native disabled state', () => {
    expect(renderToStaticMarkup(<IconButton {...props} disabled />)).toContain(' disabled=""');
  });
  it('keeps a busy control focusable and substitutes the approved decorative indicator', () => {
    const html = renderToStaticMarkup(<IconButton {...props} disabled loading />);
    expect(html).toContain('aria-busy="true"');
    expect(html).toContain('aria-disabled="true"');
    expect(html).not.toContain(' disabled=""');
    expect(html).toContain('>…</span>');
    expect(html).not.toContain('ot-icon-button-art');
  });
});
