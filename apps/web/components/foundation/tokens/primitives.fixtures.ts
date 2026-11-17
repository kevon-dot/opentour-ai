import type { BreadcrumbItem, LinkProps, LiveRegionProps } from './primitives.contracts';

/** Generated presentation examples only. No route, tenant record, grant or announcement is real. */
export const primitiveFixtures = {
  /** One example for each designed link text style; the arrow follows the drawn examples. */
  links: [
    { size: 'small', label: 'Generated details example', href: '/generated/details' },
    { size: 'small-strong', label: 'Open generated example', href: '/generated/support', trailingArrow: true },
    { size: 'label', label: 'Read generated terms', href: '/generated/terms', trailingArrow: true },
  ] as readonly LinkProps[],
  linkLongLabel: 'Open a generated example with a long descriptive name GENERATED-0123456789-0123456789-0123456789-0123456789 that wraps instead of clipping',
  breadcrumbLabel: 'Breadcrumb',
  breadcrumb: [
    { label: 'Generated tenant A', href: '/generated' },
    { label: 'Generated workflow' },
  ] as readonly BreadcrumbItem[],
  breadcrumbDeep: [
    { label: 'Generated tenant B', href: '/generated' },
    { label: 'Generated group' },
    { label: 'Generated workflow', href: '/generated/workflow' },
    { label: 'Generated details' },
  ] as readonly BreadcrumbItem[],
  breadcrumbLong: [
    { label: 'Generated tenant with a long descriptive name that wraps instead of clipping', href: '/generated' },
    { label: 'GENERATED-0123456789-0123456789-0123456789-0123456789-0123456789' },
  ] as readonly BreadcrumbItem[],
  safeDestinations: ['/generated', '/generated/a:b', './generated', '../generated', '?page=2', '#section',
    'generated/relative:path', '//generated.example/path', 'https://generated.example/path',
    'HTTP://generated.example/', 'mailto:generated@generated.example'],
  unsafeDestinations: ['javascript:alert(1)', ' JavaScript:alert(1)', 'java\tscript:alert(1)', 'java\nscript:alert(1)',
    '\u0001javascript:alert(1)', 'data:text/html,<script>generated</script>', 'vbscript:generated',
    'file:///generated', 'blob:generated', 'generated:value'],
  announcements: [
    { message: 'Generated status changed.' },
    { message: 'Generated status needs attention.', politeness: 'assertive' },
  ] as readonly LiveRegionProps[],
} as const;
