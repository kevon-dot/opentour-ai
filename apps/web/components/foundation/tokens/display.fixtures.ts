import type { MetricProps, NavItemProps, TableCellProps } from './display.contracts';

/** Generated presentation examples only. No measurement, API response, grant, or receipt is simulated. */
export const displayFixtures = {
  metricsLabel: 'Generated metric examples',
  /** One example per designed tone. The last one is an unknown value, stated in words with its reason. */
  metrics: [
    { tone: 'blue', title: 'Generated count example', value: '1,024', detail: 'of 2,048 generated examples · 50.0%' },
    { tone: 'mint', title: 'Generated share example', value: '50.0%', detail: '1,024 / 2,048 generated examples' },
    { tone: 'lilac', title: 'Generated difference example', value: '12 pp', detail: 'Generated interval: 4.0–20.0 pp' },
    { tone: 'amber', title: 'Generated unknown example', value: 'Unknown', detail: 'No generated observation was recorded.' },
  ] as readonly MetricProps[],
  metricLong: {
    tone: 'lilac',
    title: 'Generated metric title with a long descriptive name that wraps instead of clipping',
    value: 'GENERATED-0123456789-0123456789 units',
    detail: 'Generated detail with an unbroken token GENERATED-0123456789-0123456789-0123456789-0123456789 and enough words to span several lines.',
  } as MetricProps,
  tableCaption: 'Generated table example',
  /** A header row, then rows whose first cell names the row. Every designed tone appears once. */
  tableColumns: [
    { header: 'column', tone: 'blue', value: 'Generated record' },
    { header: 'column', tone: 'blue', value: 'Generated result' },
    { header: 'column', tone: 'blue', value: 'Generated next step' },
  ] as readonly TableCellProps[],
  tableRows: [
    [{ header: 'row', value: 'Generated record A' }, { value: 'Confirmed example' }, { tone: 'mint', value: 'No generated step' }],
    [{ header: 'row', value: 'Generated record B' }, { value: 'Learning example' }, { tone: 'lilac', value: 'Generated review step' }],
    [{ header: 'row', value: 'Generated record C' }, { value: 'Unknown' }, { tone: 'amber', value: 'No generated observation was recorded' }],
    [{ header: 'row', value: 'Generated record D' }, { value: 'Failed example' }, { tone: 'coral', value: 'Generated repair step' }],
    [{ header: 'row', value: 'Generated record E' }, { value: '0 of 16 generated examples' }, { tone: 'blue', value: 'Generated rebuild step' }],
  ] as readonly (readonly TableCellProps[])[],
  tableCellLong: 'Generated cell value with an unbroken token GENERATED-0123456789-0123456789-0123456789 that wraps instead of clipping',
  navigationLabel: 'Generated navigation',
  /** Labels and destinations are the caller's. Fragment destinations keep the examples on one page. */
  navigation: [
    { label: 'Generated overview', href: '#generated-overview' },
    { label: 'Generated records', href: '#generated-records', current: true },
    { label: 'Generated settings', href: '#generated-settings' },
  ] as readonly NavItemProps[],
  navItemLong: 'Generated navigation label with a long descriptive name that wraps instead of clipping',
} as const;
