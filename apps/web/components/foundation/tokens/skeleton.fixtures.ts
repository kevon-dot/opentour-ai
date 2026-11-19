/** Generated presentation fixtures only. These are not provider state or operation receipts. */
export const skeletonFixtures = {
  title: 'Static loading examples',
  description: 'Generated presentation fixture. No service request is made.',
  modes: ['customer-controlled', 'opentour-managed'],
  tenants: ['Generated tenant A', 'Generated tenant B'],
  variants: ['rows', 'card'],
  startLabel: 'Start loading',
  rerenderLabel: 'Repeat render',
  readyLabel: 'Show generated content',
  followingLabel: 'Following content stays in place.',
  loadedTitle: 'Generated fixture',
  loadedRows: ['Example A', 'Fixture', 'Example B'],
  viewports: [{ width: 320, height: 800 }, { width: 768, height: 1024 },
    { width: 1280, height: 900 }, { width: 1440, height: 1024 }],
  widths: { rows: [328, 288, 328], card: [220, 130, 280] },
  figmaNodes: { rows: '153:479', card: '153:484' },
} as const;
