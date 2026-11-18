import { renderToStaticMarkup } from 'react-dom/server';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../../tests/web-unit-cases.js';
import { IconButton } from './icon-button';

describe('IconButton', () => {
  const props = { label: 'Close receipt details', onClick: () => {} };
  it('renders one contextual native button and a decorative icon', () => {
    const html = renderToStaticMarkup(<IconButton {...props} />);
    assert.ok((html)?.includes('type="button"'));
    assert.ok((html)?.includes('aria-label="Close receipt details"'));
    assert.ok((html)?.includes('aria-hidden="true"'));
  });
  it('accepts another decorative asset and contextual description', () => {
    const html = renderToStaticMarkup(<IconButton {...props} iconSrc="/assets/search.svg" describedBy="search-help" />);
    assert.ok((html)?.includes('/assets/search.svg'));
    assert.ok((html)?.includes('aria-describedby="search-help"'));
  });
  testCases(['', '  '])('rejects an empty accessible name', label => {
    assert.throws(() => renderToStaticMarkup(<IconButton {...props} label={label} />), /accessible name/);
  });
  it('uses the native disabled state', () => {
    assert.ok((renderToStaticMarkup(<IconButton {...props} disabled />))?.includes(' disabled=""'));
  });
  it('keeps a busy control focusable and substitutes the approved decorative indicator', () => {
    const html = renderToStaticMarkup(<IconButton {...props} disabled loading />);
    assert.ok((html)?.includes('aria-busy="true"'));
    assert.ok((html)?.includes('aria-disabled="true"'));
    assert.ok((html)?.includes(' disabled=""') === false);
    assert.ok((html)?.includes('>…</span>'));
    assert.ok((html)?.includes('ot-icon-button-art') === false);
  });
});
