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
  /** `id` of the form that owns the inputs. Needed only when the summary is rendered outside that form. */
  formId?: string;
}

interface Row { key: string; text: string; fieldId?: string }

/** Rows in the order given, identical rows once, and the number of distinct fields that have a problem. */
function summarize(errors: readonly FormErrorSummaryItem[]): { rows: Row[]; fieldCount: number } {
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
  return { rows: [...rows.values()], fieldCount: fields.size };
}

/** Checked by shape, not by constructor: the form may live in another document, such as a frame. */
function isForm(node: Element | null): node is HTMLFormElement {
  return node !== null && node.localName === 'form' && 'elements' in node;
}

/**
 * The input for a link, looked up from the link's own tree at the moment of activation. The owning
 * form is searched first, including controls it owns from elsewhere through the `form` attribute, so
 * a repeated id in another form or region is not chosen. The whole tree is only the last resort.
 */
function resolveField(link: HTMLAnchorElement, fieldId: string, formId: string | undefined): HTMLElement | null {
  const root = link.getRootNode() as Node & { getElementById?: (id: string) => HTMLElement | null };
  const named = formId === undefined ? null : root.getElementById?.(formId) ?? null;
  const form = isForm(named) ? named : link.closest('form');
  if (form) {
    for (const control of form.elements) if (control.id === fieldId) return control as HTMLElement;
    for (const node of form.querySelectorAll<HTMLElement>('[id]')) if (node.id === fieldId) return node;
  }
  return root.getElementById?.(fieldId) ?? null;
}

function moveToField(event: MouseEvent<HTMLAnchorElement>, fieldId: string, formId: string | undefined): void {
  // A modified or non-primary activation keeps its native meaning, for example opening a new tab.
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const target = resolveField(event.currentTarget, fieldId, formId);
  if (!target) return;
  target.focus();
  // Native fragment navigation would pick the first matching id in the document, so it is replaced
  // only once the intended input holds focus. Otherwise the plain link behaviour is left in place.
  if (target.matches(':focus')) event.preventDefault();
}

/**
 * Linked summary of the problems that stopped a form from continuing (Figma 153:386): the failure
 * notice, then one row per problem. It is a named region that can take focus but is not a Tab stop.
 * The notice is a polite status, so a later change of the count is announced without moving focus.
 */
export function FormErrorSummary({ errors, submissionId, formId }: FormErrorSummaryProps) {
  const summary = useRef<HTMLElement>(null);
  const handled = useRef<string | number | null>(null);
  const { rows, fieldCount } = summarize(errors);
  const present = errors.length > 0;

  useEffect(() => {
    if (submissionId == null || submissionId === handled.current) return;
    // The identifier is spent even when it arrived without errors, so a later blur or edit that
    // produces errors cannot be mistaken for that submission.
    handled.current = submissionId;
    if (present) summary.current?.focus();
  }, [submissionId, present]);

  if (!present) return null;
  const title = fieldCount > 0
    ? `Check ${fieldCount} ${fieldCount === 1 ? 'field' : 'fields'} before continuing`
    : 'We could not save these changes';
  const body = fieldCount > 0
    ? 'Your entries are still here. Correct the fields below.'
    : 'Review the form and try again. Contact support if the problem continues.';

  return <section ref={summary} className="ot-form-error-summary" aria-label={title} tabIndex={-1}>
    <Notice tone="failure" title={title} body={body} />
    {rows.length > 0 && <ul className="ot-form-error-list" role="list">
      {rows.map(({ key, text, fieldId }) => <li key={key} className="ot-form-error-item">
        {fieldId === undefined ? text : <a className="ot-form-error-link" href={'#' + encodeURIComponent(fieldId)}
          onClick={event => moveToField(event, fieldId, formId)}>{text}</a>}
      </li>)}
    </ul>}
  </section>;
}
