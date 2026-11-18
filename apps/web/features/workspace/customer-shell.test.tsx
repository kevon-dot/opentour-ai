import { readFileSync } from 'node:fs';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
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
  it.each(['Customer controlled', 'OpenTour managed'] as const)('renders caller-supplied %s context, links and landmarks', mode => {
    const html = renderToStaticMarkup(<CustomerShell {...props} context={{ ...props.context, mode }} />);
    for (const content of [mode, props.context.origin, props.context.cloud, props.context.region,
      'Skip to main content', '<main ', '<h1>Refund status</h1>', 'role="tablist"', 'Workspace utilities',
      ...links.map(item => item.label), props.delivery.detail, props.page.primaryAction.label]) expect(html).toContain(content);
    expect(html).toContain('href="#view-0" aria-current="page"');
  });
  it('prints a full long workspace identity as text', () => {
    const customer = 'Northstar Research and Development & Customer Operations';
    const html = renderToStaticMarkup(<CustomerShell {...props} workspace={{ customer, scope: 'Production workspace' }} />);
    expect(html).toContain('Northstar Research and Development &amp; Customer Operations');
  });
  it('contains no product routing, external reads or persistence dependencies', () => {
    const implementation = readFileSync(new URL('./customer-shell.tsx', import.meta.url), 'utf8');
    expect(implementation).not.toMatch(/\b(fetch|XMLHttpRequest|WebSocket|localStorage|sessionStorage|indexedDB)\b|next\/|authkit/);
  });
});
