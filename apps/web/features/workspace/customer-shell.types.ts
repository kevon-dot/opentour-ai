import type { BreadcrumbProps } from '../../components/foundation/tokens/primitives';
import type { TabsProps } from '../../components/foundation/tokens';

export interface ShellLink { label: string; href: string }

export interface CustomerShellProps {
  brand: string;
  workspace: { customer: string; scope: string };
  navigation: readonly ShellLink[];
  activeHref: string;
  utilities: readonly [ShellLink, ShellLink, ShellLink];
  delivery: { title: string; detail: string };
  profile: string;
  breadcrumb: BreadcrumbProps;
  context: { origin: string; mode: 'Customer controlled' | 'OpenTour managed'; cloud: string; region: string };
  page: { title: string; subtitle: string; primaryAction: { label: string; onPress(): void } };
  tabs: TabsProps;
}
