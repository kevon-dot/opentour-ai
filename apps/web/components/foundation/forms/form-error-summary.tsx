'use client';

import { useEffect, useRef } from 'react';
import type { MouseEvent } from 'react';
import { Notice } from '../tokens/notice';
import { requireLabel } from '../tokens/require-label';
import './form-error-summary.css';

/**
 * One current problem. A mapped problem belongs to one registered input and becomes a link to it.
 * An unmapped problem belongs to no single field: its optional `message` is shown as plain text.
 * Every string is presentation copy written for the person filling the form. Never pass a raw
 * provider, server or exception message; omit `message` and the general copy stands alone.
 */
export type FormErrorSummaryItem =
  | {
    /** Native `id` of the registered input, exactly as rendered now. */
    fieldId: string;
    /** Visible name of the field. */
    fieldLabel: string;
    /** Short instruction shown after the field name. */
    message: string;
  }
  | { fieldId?: undefined; fieldLabel?: undefined; message?: string };

/** Controlled presentation. The summary holds no errors and no values of its own and never edits a draft. */
export interface FormErrorSummaryProps {
  /** The current problems in field order. An empty list renders nothing. */
  errors: readonly FormErrorSummaryItem[];
  /**
   * Identifier of the latest submission that failed, or null before one. Change it in the same update
   * that supplies that submission's errors. Focus moves to the summary once for each new identifier
   * that arrives with errors, including a repeat of the same errors. Nothing else moves focus.
   */
  submissionId: string | number | null;
  /** `id` of the form that owns the inputs. Needed only when the rows are rendered outside that form. */
  formId?: string;
  /**
   * Where the rows are drawn. `attached`, the default, keeps them directly under the notice (Figma 153:386).
   * `detached` leaves them out, so the owning form can place its fields between this notice and one
   * `FormErrorSummaryRows` that is given the same `errors` (Figma 159:121).
   */
  rows?: 'attached' | 'detached';
}

/** The rows of a detached summary. Pass the same `errors` as the summary so both describe one state. */
export interface FormErrorSummaryRowsProps {
  errors: readonly FormErrorSummaryItem[];
  /** `id` of the form that owns the inputs. Needed only when the rows are rendered outside that form. */
  formId?: string;
}

interface Row { key: string; text: string; fieldId?: string }

/**
 * Everything either part shows, from the errors alone: the rows in the order given with identical rows
 * once, and the heading and body that follow the number of distinct fields that have a problem.
 */
function summarize(errors: readonly FormErrorSummaryItem[]): { rows: Row[]; title: string; body: string } {
  const rows = new Map<string, Row>();
  const fields = new Set<string>();
  for (const error of errors) {
    if (error.fieldId !== undefined) {
      requireLabel(error.fieldId, 'form error field identity');
      if (/\s/.test(error.fieldId)) throw new Error('A form error field identity is a native id without spaces.');
      requireLabel(error.fieldLabel, 'form error field name');
      requireLabel(error.message, 'form error message');
      fields.add(error.fieldId);
      const text = `${error.fieldLabel}: ${error.message}`;
      const key = JSON.stringify([error.fieldId, text]);
      if (!rows.has(key)) rows.set(key, { key, text, fieldId: error.fieldId });
    } else if (error.message !== undefined) {
      requireLabel(error.message, 'form error message');
      const key = JSON.stringify([null, error.message]);
      if (!rows.has(key)) rows.set(key, { key, text: error.message });
    }
  }
  const fieldCount = fields.size;
  return {
    rows: [...rows.values()],
    title: fieldCount > 0
      ? `Check ${fieldCount} ${fieldCount === 1 ? 'field' : 'fields'} before continuing`
      : 'We could not save these changes',
    body: fieldCount > 0
      ? 'Your entries are still here. Correct the fields below.'
      : 'Review the form and try again. Contact support if the problem continues.',
  };
}

/** Checked by shape, not by constructor: the form may live in another document, such as a frame. */
function isForm(node: Element | null): node is HTMLFormElement {
  return node !== null && node.localName === 'form' && 'elements' in node;
}

/** The element with this id that the form owns: a control it lists, or other content inside it. */
function findOwned(form: HTMLFormElement, fieldId: string): HTMLElement | null {
  // Listed controls include those the form owns from elsewhere through the `form` attribute.
  for (const control of form.elements) if (control.id === fieldId) return control as HTMLElement;
  for (const node of form.querySelectorAll<HTMLElement>('[id]')) {
    if (node.id !== fieldId) continue;
    // A control inside this form's markup can be owned by another form through its own `form` attribute.
    const owner = (node as HTMLElement & { form?: HTMLFormElement | null }).form;
    if (owner == null || owner === form) return node;
  }
  return null;
}

/**
 * The input for a link, looked up from the link's own tree at the moment of activation.
 *
 * A link has a known owner when a `formId` was given or when the link sits inside a form. The named
 * form wins and the enclosing form is not a substitute for it. A known owner is the only place that
 * is searched: when it no longer holds the input, or the named form itself is gone, there is no
 * target. Another form or the rest of the document is never searched instead, so a repeated id
 * elsewhere cannot be chosen. Only a link with no owner at all is resolved from its whole tree.
 */
function resolveField(link: HTMLAnchorElement, fieldId: string, formId: string | undefined):
  { owned: boolean; target: HTMLElement | null } {
  const root = link.getRootNode() as Node & { getElementById?: (id: string) => HTMLElement | null };
  if (formId !== undefined) {
    const named = root.getElementById?.(formId) ?? null;
    return { owned: true, target: isForm(named) ? findOwned(named, fieldId) : null };
  }
  const enclosing = link.closest('form');
  if (enclosing) return { owned: true, target: findOwned(enclosing, fieldId) };
  return { owned: false, target: root.getElementById?.(fieldId) ?? null };
}

function moveToField(event: MouseEvent<HTMLAnchorElement>, fieldId: string, formId: string | undefined): void {
  // A modified or non-primary activation keeps its native meaning, for example opening a new tab.
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const { owned, target } = resolveField(event.currentTarget, fieldId, formId);
  // Native fragment navigation picks the first matching id in the whole document, which can sit in
  // another form. For an owned link it therefore never runs, whether the input is found, missing or
  // unable to take focus. When there is no target, focus stays on the link.
  if (owned) event.preventDefault();
  if (!target) return;
  target.focus();
  // Without an owner the native target is the same element, so native behaviour is replaced only
  // once that element holds focus and is otherwise left in place.
  if (!owned && target.matches(':focus')) event.preventDefault();
}

/** The one implementation of the rows, shared by the attached and the detached arrangement. */
function RowList({ rows, formId, label }: { rows: readonly Row[]; formId: string | undefined; label?: string }) {
  if (rows.length === 0) return null;
  return <ul className={label === undefined ? 'ot-form-error-list' : 'ot-form-error-list ot-form-error-list--detached'}
    role="list" aria-label={label}>
    {rows.map(({ key, text, fieldId }) => <li key={key} className="ot-form-error-item">
      {fieldId === undefined ? text : <a className="ot-form-error-link" href={'#' + encodeURIComponent(fieldId)}
        onClick={event => moveToField(event, fieldId, formId)}>{text}</a>}
    </li>)}
  </ul>;
}

/**
 * Linked summary of the problems that stopped a form from continuing (Figma 153:386): the failure
 * notice, then one row per problem. It is a named region that can take focus but is not a Tab stop.
 * The notice is a polite status, so a later change of the count is announced without moving focus.
 * With `rows="detached"` the region holds the notice alone and `FormErrorSummaryRows` draws the rows.
 */
export function FormErrorSummary({ errors, submissionId, formId, rows: placement = 'attached' }: FormErrorSummaryProps) {
  const summary = useRef<HTMLElement>(null);
  const handled = useRef<string | number | null>(null);
  const { rows, title, body } = summarize(errors);
  const present = errors.length > 0;

  useEffect(() => {
    if (submissionId == null || submissionId === handled.current) return;
    // The identifier is spent even when it arrived without errors, so a later blur or edit that
    // produces errors cannot be mistaken for that submission.
    handled.current = submissionId;
    if (present) summary.current?.focus();
  }, [submissionId, present]);

  if (!present) return null;
  return <section ref={summary} className="ot-form-error-summary" aria-label={title} tabIndex={-1}>
    <Notice tone="failure" title={title} body={body} />
    {placement === 'attached' && <RowList rows={rows} formId={formId} />}
  </section>;
}

/**
 * The rows of a detached summary, placed after the fields they point to (Figma 159:121). The list is
 * named by the summary's heading because it no longer sits under it. It adds no notice, no status,
 * no region and no focus movement of its own, and renders nothing when there is no row to show.
 */
export function FormErrorSummaryRows({ errors, formId }: FormErrorSummaryRowsProps) {
  const { rows, title } = summarize(errors);
  return <RowList rows={rows} formId={formId} label={title} />;
}
