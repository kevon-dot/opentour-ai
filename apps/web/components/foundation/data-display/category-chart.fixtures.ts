import type { CategoryChartProps } from './category-chart';
import type { ObservedCount } from './chart-evidence.types';

const observed = (count: number): ObservedCount => ({ status: 'observed', count });
const category = (label: string, count: number) => ({ key: label, label, value: observed(count) });
export const categoryFixtures = {
  overflow: {
    visibleCategoryLimit: 3, totalCases: 100, other: observed(20),
    categories: [category('A', 50), category('B', 30), category('C', 10), category('D', 7), category('E', 3)],
  },
  tied: {
    visibleCategoryLimit: 3, totalCases: 8,
    categories: [category('Zulu', 4), category('Alpha', 4), category('No matching cases', 0)],
  },
  incomplete: {
    visibleCategoryLimit: 3, totalCases: 10, other: { status: 'unknown', reason: 'Aggregate unavailable' },
    categories: [category('Lengthy category label across narrow widths', 3), category('Known zero', 0),
      { key: 'pending', label: 'Pending count', value: { status: 'missing', reason: 'Source unavailable' } },
      { key: 'unknown', label: 'Unknown count', value: { status: 'unknown', reason: 'Count not supplied' } }],
  },
} as const satisfies Record<string, Omit<CategoryChartProps, 'expanded' | 'onExpandedChange'>>;
