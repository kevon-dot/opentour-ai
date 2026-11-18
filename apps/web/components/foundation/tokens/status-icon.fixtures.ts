import type { StatusIconProps } from './status-icon.contracts';

/** Generated presentation fixtures; labels do not establish a real operation or runtime outcome. */
export const statusIconFixtures = {
  title: 'Status icon · always paired with exact text',
  disclaimer: 'Generated presentation examples. No operation or provider result is claimed.',
  modes: ['customer-controlled', 'opentour-managed'],
  viewports: [{ width: 320, height: 800 }, { width: 768, height: 1024 },
    { width: 1280, height: 900 }, { width: 1440, height: 1024 }],
  examples: [
    { tone: 'success', label: 'Completed' }, { tone: 'hold', label: 'On hold' },
    { tone: 'failed', label: 'Failed' }, { tone: 'unknown', label: 'Unknown' },
    { tone: 'info', label: 'Information' },
  ] satisfies StatusIconProps[],
  nonstandard: { tone: 'info', label: 'Awaiting a generated review' } satisfies StatusIconProps,
  long: { tone: 'unknown', label: 'Generated observation remains unconfirmed while a longer descriptive status wraps without losing its exact words.' } satisfies StatusIconProps,
  beforeLabel: 'Before generated statuses', afterLabel: 'After generated statuses', rerenderLabel: 'Repeat presentation render',
  figmaNodes: { success: '162:4569', hold: '162:4572', failed: '162:4575', unknown: '162:4578', info: '162:4581' },
} as const;
