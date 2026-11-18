/**
 * OPE-319 presentation inputs only. Every string is an already-authorized display string supplied
 * by the caller: it is rendered verbatim (escaped by React), never parsed, reformatted, counted or
 * inferred from. No prop carries server authority, graph identity, hidden data or an action.
 */

/** Fill colour only, as drawn in 16:81. It never states a condition; the visible version text does. */
export type RelationshipVersionTone = 'default' | 'mint';

/** One row of the read-only relationship records table (16:81: Node · Relationship · Version). */
export interface RelationshipRecordRow {
  /** Stable caller key; used only for React reconciliation and never rendered. */
  readonly key: string;
  readonly node: string;
  readonly relationship: string;
  /** Literal version or lifecycle text such as `12` or `Sealed`. */
  readonly version: string;
  readonly versionTone?: RelationshipVersionTone;
}

/** One row of the selected-relationship table (165:11224 family: Source · Relation · Target · Version · Validity). */
export interface SelectedRelationshipRow {
  /** Stable caller key; used only for React reconciliation and never rendered. */
  readonly key: string;
  readonly source: string;
  readonly relation: string;
  readonly target: string;
  /** Literal version text, including arrows or an explicit `Not available`. */
  readonly version: string;
  /** Literal validity text, including an explicit `Unknown`. */
  readonly validity: string;
}

/** The currently selected node's read-only details (165:11243 family, without its actions). */
export interface SelectedNodeContext {
  readonly title: string;
  readonly note: string;
  /** Shown under the fixed `Exact reference` label. */
  readonly reference: string;
  /** Supporting reference text shown verbatim, for example a shortened digest. */
  readonly referenceDetail: string;
}

/** At least one row. Zero rows are not a data table: the caller states `empty` explicitly. */
export type NonEmptyRows<Row> = readonly [Row, ...Row[]];

/**
 * Closed display states. `empty` and `redacted` carry no data at all: their public copy and
 * placeholder rows are fixed, so hidden names, types or counts cannot be passed in to be hidden.
 */
export type RelationshipContextProps =
  | {
    readonly view: 'records';
    readonly rows: NonEmptyRows<RelationshipRecordRow>;
    /** Caller-supplied observation line, for example `6 records · observed 09:42 UTC`. */
    readonly observation: string;
  }
  | {
    readonly view: 'selected';
    readonly rows: NonEmptyRows<SelectedRelationshipRow>;
    /** The current selection's details, or `null` when nothing is selected. Never cached. */
    readonly selected: SelectedNodeContext | null;
  }
  | { readonly view: 'empty' }
  | { readonly view: 'redacted' };
