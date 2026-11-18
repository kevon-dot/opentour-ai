import { readFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { testCases } from '../../tests/web-unit-cases.js';
import { CustomerShell } from './customer-shell';
import type { CustomerShellProps } from './customer-shell.types';

const links = ['Overview', 'Workflows', 'Sources', 'Learning assets', 'Experiments', 'Benchmarks', 'Releases', 'Delivery']
  .map((label, index) => ({ label, href: '#view-' + index }));
const props: CustomerShellProps = {
  brand: 'OpenTour', workspace: { customer: 'Acme Labs', scope: 'Production workspace' },
  navigation: links, activeHref: links[0].href,
  utilities: [{ label: 'Environment', href: '#environment' }, { label: 'Usage', href: '#usage' }, { label: 'Settings', href: '#settings' }],
  delivery: { title: 'Your delivery team', detail: 'White-glove support is included.' }, profile: 'JD  Jordan Davis',
  breadcrumb: { label: 'Breadcrumb', items: [{ label: 'Acme Labs', href: '#workspaces' }, { label: 'Production' }] },
  context: { origin: 'Demo data', mode: 'Customer controlled', cloud: 'AWS', region: 'us-east-1' },
  page: { title: 'Refund status', subtitle: 'Refund status · receipt-backed answers · policy v2',
    primaryAction: { label: 'View benchmark', onPress() {} } },
  tabs: { label: 'Page tabs', selectedKey: 'overview', items: [{ key: 'overview', label: 'Overview', panel: 'Panel' }], onSelectionChange() {} },
};
describe('customer shell presentation', () => {
  testCases(['Customer controlled', 'OpenTour managed'] as const)('renders caller-supplied %s context, links and landmarks', mode => {
    const html = renderToStaticMarkup(<CustomerShell {...props} context={{ ...props.context, mode }} />);
    for (const content of [mode, props.context.origin, props.context.cloud, props.context.region,
      'Skip to main content', '<main ', '<h1>Refund status</h1>', 'role="tablist"', 'Workspace utilities',
      ...links.map(item => item.label), props.delivery.detail, props.page.primaryAction.label]) assert.ok((html)?.includes(content));
    assert.ok((html)?.includes('href="#view-0" aria-current="page"'));
  });
  testCases(props.utilities)('marks the active utility $label as the current page', ({ href }) => {
    const html = renderToStaticMarkup(<CustomerShell {...props} activeHref={href} />);
    assert.ok((html)?.includes('href="' + href + '" aria-current="page"'));
    assert.ok((html)?.includes('href="#view-0" aria-current="page"') === false);
  });
  it('prints a full long workspace identity as text', () => {
    const customer = 'Northstar Research and Development & Customer Operations';
    const html = renderToStaticMarkup(<CustomerShell {...props} workspace={{ customer, scope: 'Production workspace' }} />);
    assert.ok((html)?.includes('Northstar Research and Development &amp; Customer Operations'));
  });
  it('contains no product routing, external reads or persistence dependencies', () => {
    const implementation = readFileSync(new URL('./customer-shell.tsx', import.meta.url), 'utf8');
    assert.doesNotMatch(implementation, /\b(fetch|XMLHttpRequest|WebSocket|localStorage|sessionStorage|indexedDB)\b|next\/|authkit/);
  });
});
