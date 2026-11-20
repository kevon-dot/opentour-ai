import assert from 'node:assert/strict';
import { it } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { ResourceDisclosure } from './resource-disclosure';

const known = { disclosure: 'known' as const, actionLabel: 'Start evaluation',
  safeReason: 'Your role does not include the evaluation capability.',
  authorizedContent: <span title="Private help">Private workflow · 300</span> };

it('ties the focusable action to its caller-supplied visible explanation', () => {
  const html = renderToStaticMarkup(<ResourceDisclosure {...known} />);
  const reasonId = /aria-describedby="([^"]+)"/.exec(html)?.[1];
  assert.ok(reasonId);
  assert.ok(html.includes('id="' + reasonId + '">' + known.safeReason + '</p>'));
  assert.match(html, /type="button" aria-disabled="true"[^>]*>Start evaluation/);
  assert.doesNotMatch(html, / disabled=""|tabindex="-1"/i);
  assert.throws(() => renderToStaticMarkup(<ResourceDisclosure {...known} safeReason=" " />));
});

it('uses only the generic heading, notice and return action even with stale fields', () => {
  const stale = { ...known, disclosure: 'undisclosed' as const, onReturnToWorkspace() {} };
  const html = renderToStaticMarkup(<ResourceDisclosure {...stale} />);
  assert.match(html, /<h1[^>]*>Resource not found<\/h1>/);
  assert.match(html, /This resource is unavailable in the current context/);
  assert.match(html, /Return to workspace/);
  assert.doesNotMatch(html, /Private|300|evaluation capability|Start evaluation|aria-describedby/);
});
