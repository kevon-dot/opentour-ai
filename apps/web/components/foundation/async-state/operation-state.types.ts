import type { BadgeTone, DataStateKind, NoticeTone, ProgressState } from '../tokens';

/** Local presentation values. The caller supplies the observation and all visible copy. */
interface Observation {
  readonly resource: string;
  readonly observedAt: string;
  readonly status: string;
}

type ProgressDisplay = { readonly kind: 'progress'; readonly state: ProgressState; readonly detail: string };
type NoticeDisplay = { readonly kind: 'notice'; readonly tone: NoticeTone; readonly detail: string };
type DataDisplay = { readonly kind: 'data'; readonly state: DataStateKind; readonly detail: string };
type BadgeDisplay = { readonly kind: 'badge'; readonly tone: BadgeTone; readonly detail?: string };

export interface OperationStateProps {
  readonly operation: Observation & (ProgressDisplay | NoticeDisplay | DataDisplay);
  readonly decision: Observation & (NoticeDisplay | BadgeDisplay | DataDisplay);
  readonly runtime: Observation & (BadgeDisplay | DataDisplay);
}
