export type TableRecord = Readonly<{
  id: string;
  label: string;
  lifecycle: string;
  observed: string;
}>;

export type TableSelection =
  | { mode: 'page'; ids: readonly string[] }
  | { mode: 'query'; revision: string; count: number; visibleIds: readonly string[] };

export type TableIntent =
  | { type: 'filter' | 'clear' | 'connect' | 'sort' | 'previous' | 'next' | 'review' | 'select-page' | 'select-query' | 'clear-selection' }
  | { type: 'search'; value: string }
  | { type: 'page-size'; value: number }
  | { type: 'inspect' | 'select-row'; id: string };

export type TablePopulationsProps = {
  caption: string;
  context: string;
  search: string;
  filters: readonly string[];
  total: number | null;
  matching: number | null;
  selection: TableSelection;
  direction: 'ascending' | 'descending';
  pageSize: number;
  pageSizes: readonly number[];
  position: string;
  previous: boolean;
  next: boolean;
  onIntent: (intent: TableIntent) => void;
} & (
  | { state: 'populated'; rows: readonly TableRecord[] }
  | { state: 'partial'; rows: readonly TableRecord[]; expected: number | null; reason: string }
  | { state: 'empty' | 'filtered-to-nothing'; rows: readonly [] }
);

/** Operation-list pagination specimen 31:103 (page selection, frozen IDs). */
export type PageSelectionNavigationProps = {
  selectedIds: readonly string[];
  queryCount: number;
  revision: string;
  page: number;
  pages: number;
  pageSize: number;
  observed: string;
  search: string;
  previous: boolean;
  next: boolean;
  onIntent: (intent: TableIntent) => void;
};
