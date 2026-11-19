'use client';

import { useId, useLayoutEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import type { BadgeProps, ButtonProps, ModalPresentationProps, TabsProps } from './contracts';
import { requireLabel } from './require-label';
import '../../../styles/product-tokens.css';

export type { BadgeProps, BadgeTone, ButtonProps, ButtonVariant, DismissReason, InitialFocus, ModalPresentationProps, NoticeProps, NoticeTone, TabItem, TabsProps } from './contracts';
export { Notice, noticeTones } from './notice';
export type { DataStateKind, DataStateProps, PersistentAnnouncement, ProgressProps, ProgressState, ToastAnnouncement,
  ToastProps, ToastState } from './feedback.contracts';
export { DataState, Progress, Toast, dataStates, progressStates, toastStates } from './feedback';
export type { MetricProps, MetricTone, NavItemProps, TableCellProps, TableCellTone } from './display.contracts';
export { Metric, NavItem, TableCell, metricTones, tableCellTones } from './display';
export { Skeleton } from './skeleton';
export type { SkeletonProps, SkeletonVariant } from './skeleton.contracts';
export { StatusIcon, statusIconTones } from './status-icon';
export type { StatusIconProps, StatusIconTone } from './status-icon.contracts';
export { Checkbox, Switch } from './selection-controls';
export type { CheckboxProps, CheckboxState, SwitchProps, SwitchState } from './selection-controls.contracts';

const lightIndicator = new URL('./assets/loading-light.svg', import.meta.url).href;
const darkIndicator = new URL('./assets/loading-dark.svg', import.meta.url).href;

export function Button({ label, variant = 'primary', type = 'button', disabled = false,
  busy = false, disabledReason, onPress, ref }: ButtonProps) {
  requireLabel(label);
  const reasonId = useId();
  const blocked = disabled || busy;
  return <span className="ot-button-group">
    <button ref={ref} type={type} className={`ot-button ot-button--${variant}`}
      disabled={disabled} aria-disabled={blocked || undefined} aria-busy={busy || undefined}
      aria-describedby={blocked && disabledReason ? reasonId : undefined}
      onClick={event => {
        if (blocked) { event.preventDefault(); return; }
        onPress?.(event);
      }}>
      {busy && <img className="ot-button-indicator" src={variant === 'primary' ? lightIndicator : darkIndicator}
        width={12} height={12} alt="" aria-hidden="true" />}
      <span>{label}</span>
    </button>
    {blocked && disabledReason && <span id={reasonId} className="ot-button-reason">{disabledReason}</span>}
  </span>;
}

export function Badge({ label, tone }: BadgeProps) {
  requireLabel(label);
  return <span className={`ot-badge ot-badge--${tone}`}>{label}</span>;
}

export function Tabs({ label, items, selectedKey, onSelectionChange }: TabsProps) {
  requireLabel(label);
  const prefix = useId();
  const [focusedKey, setFocusedKey] = useState(selectedKey);
  const [focusWithin, setFocusWithin] = useState(false);
  const refs = useRef(new Map<string, HTMLButtonElement>());
  const enabled = items.filter(item => !item.disabled);
  if (!enabled.some(item => item.key === selectedKey) ||
    new Set(items.map(item => item.key)).size !== items.length ||
    items.some(item => !item.key || typeof item.key !== 'string')) {
    throw new Error('Tabs require unique keys and an enabled selected item.');
  }
  for (const item of items) requireLabel(item.label);
  const stopKey = focusWithin && enabled.some(item => item.key === focusedKey) ? focusedKey : selectedKey;
  const tabId = (key: string) => `${prefix}-tab-${encodeURIComponent(key)}`;
  const panelId = (key: string) => `${prefix}-panel-${encodeURIComponent(key)}`;

  function navigate(event: KeyboardEvent<HTMLButtonElement>, key: string) {
    const index = enabled.findIndex(item => item.key === key);
    const rtl = getComputedStyle(event.currentTarget).direction === 'rtl';
    let next: number;
    switch (event.key) {
      case 'ArrowRight': next = (index + (rtl ? -1 : 1) + enabled.length) % enabled.length; break;
      case 'ArrowLeft': next = (index + (rtl ? 1 : -1) + enabled.length) % enabled.length; break;
      case 'Home': next = 0; break;
      case 'End': next = enabled.length - 1; break;
      default: return; // Native Enter/Space dispatches click; focus movement never selects.
    }
    event.preventDefault();
    refs.current.get(enabled[next].key)?.focus();
  }

  return <div className="ot-tabs">
    <div role="tablist" aria-label={label} aria-orientation="horizontal" className="ot-tablist"
      onFocus={() => setFocusWithin(true)}
      onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setFocusWithin(false); }}>
      {items.map(item => <button key={item.key} type="button" role="tab" id={tabId(item.key)}
        ref={node => { if (node) refs.current.set(item.key, node); else refs.current.delete(item.key); }}
        className="ot-tab" disabled={item.disabled} aria-selected={selectedKey === item.key}
        aria-controls={panelId(item.key)} tabIndex={!item.disabled && stopKey === item.key ? 0 : -1}
        onFocus={() => setFocusedKey(item.key)} onKeyDown={event => navigate(event, item.key)}
        onClick={() => { if (!item.disabled && item.key !== selectedKey) onSelectionChange(item.key); }}>
        {item.label}
      </button>)}
    </div>
    {items.map(item => <div key={item.key} role="tabpanel" id={panelId(item.key)}
      aria-labelledby={tabId(item.key)} hidden={selectedKey !== item.key} tabIndex={0} className="ot-tabpanel">
      {selectedKey === item.key ? item.panel : null}
    </div>)}
  </div>;
}

// One owner per document. Nested overlays are intentionally outside this presentation contract.
const modalOwners = new WeakMap<Document, symbol>();

function availableFocusTarget(node: HTMLElement | null): node is HTMLElement {
  return !!node && node.isConnected && !node.closest('[hidden], [inert], [aria-hidden="true"], [aria-disabled="true"]') &&
    !node.matches(':disabled, input[type="hidden"]') && node.checkVisibility({ checkVisibilityCSS: true });
}

function focusTarget(node: HTMLElement | null): boolean {
  if (!availableFocusTarget(node)) return false;
  node.focus({ preventScroll: true });
  return node.ownerDocument.activeElement === node;
}

/** A closed dialog is not rendered, so visibility is judged from the target's own chain up to the dialog. */
function focusableOnceOpen(node: HTMLElement | null, dialog: HTMLDialogElement): node is HTMLElement {
  if (!node || node === dialog || !dialog.contains(node) ||
    !node.matches('button, a[href], input, select, textarea, summary, [tabindex], [contenteditable=""], [contenteditable="true"]') ||
    node.closest('[hidden], [inert], [aria-hidden="true"], [aria-disabled="true"]') ||
    node.matches(':disabled, input[type="hidden"]')) return false;
  const view = node.ownerDocument.defaultView;
  if (!view || view.getComputedStyle(node).visibility !== 'visible') return false;
  for (let current: HTMLElement | null = node; current && current !== dialog; current = current.parentElement) {
    if (view.getComputedStyle(current).display === 'none') return false;
  }
  return true;
}

type FocusResolvers = Pick<ModalPresentationProps, 'returnFocus' | 'fallbackFocus'>;

function ModalContent({ title, children, actions, initialFocus, onDismiss, kind, titleId, resolvers }: Omit<ModalPresentationProps, 'open' | 'returnFocus' | 'fallbackFocus'> & {
  kind: 'dialog' | 'drawer'; titleId: string; resolvers: { current: FocusResolvers };
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState<'none' | 'inline' | 'block'>('none');
  // Initial focus is sampled only for this opening, never on an ordinary rerender.
  const openingFocus = useRef(initialFocus);
  useLayoutEffect(() => {
    const dialog = dialogRef.current!;
    const heading = headingRef.current!;
    const document = dialog.ownerDocument;
    // :modal alone also matches a Fullscreen API element, which is not another modal presentation.
    if (document.querySelector('dialog:modal') && !dialog.matches(':modal')) {
      throw new Error('Nested modal presentation is not supported.');
    }
    const owner = Symbol('foundation-modal');
    modalOwners.set(document, owner);
    dialog.setAttribute('closedby', 'none');
    if (!dialog.open) {
      const requested = openingFocus.current.kind === 'element' ? openingFocus.current.ref.current :
        openingFocus.current.kind === 'close-button' ? closeRef.current : heading;
      const target = focusableOnceOpen(requested, dialog) ? requested : heading;
      // showModal() runs the platform focusing steps exactly once. Point them at the one
      // target for this opening instead of focusing the heading and then moving on.
      const marked = target.hasAttribute('autofocus');
      target.setAttribute('autofocus', '');
      try { dialog.showModal(); } finally { if (!marked) target.removeAttribute('autofocus'); }
      // Only if the platform could not focus that target: one corrective move, heading last.
      if (document.activeElement !== target && !focusTarget(target)) focusTarget(heading);
    }
    return () => {
      // React removes the dialog after layout cleanup. Native removing steps release the
      // top layer without close()'s unsafe restoration of its opening-time origin.
      queueMicrotask(() => {
        if (dialog.isConnected || modalOwners.get(document) !== owner) return;
        modalOwners.delete(document);
        if (document.querySelector('dialog:modal')) return;
        // Restore only focus that is actually lost. A target the caller has already focused
        // between removal and this microtask is the caller's decision and is left alone.
        const active = document.activeElement;
        if (active && active !== document.body && active !== document.documentElement && !dialog.contains(active)) return;
        for (const resolve of [resolvers.current.returnFocus, resolvers.current.fallbackFocus]) {
          try { if (focusTarget(resolve())) return; } catch { /* Invalid consumer targets cannot prevent teardown. */ }
        }
        // Neither target is usable: cleanup still completes. This is not successful
        // accessible focus return; consumer qualification must detect and correct it.
      });
    };
  }, [resolvers]);

  // The body is a keyboard stop only while it actually scrolls; declared after the opening
  // effect so the first measurement sees the rendered dialog.
  useLayoutEffect(() => {
    const body = bodyRef.current!;
    const measure = () => setOverflow(body.scrollHeight > body.clientHeight ? 'block' : body.scrollWidth > body.clientWidth ? 'inline' : 'none');
    const resize = new ResizeObserver(measure);
    const observe = () => { resize.disconnect(); resize.observe(body); for (const child of body.children) resize.observe(child); };
    // Content can change without this component rendering again; follow its direct children.
    const mutation = new MutationObserver(records => {
      if (records.some(record => record.type === 'childList' && record.target === body)) observe();
      measure();
    });
    observe(); measure();
    // Nested style changes can alter the scroll extent without resizing the body or its direct children.
    mutation.observe(body, { attributes: true, childList: true, subtree: true, characterData: true });
    return () => { resize.disconnect(); mutation.disconnect(); };
  }, []);

  function handleKey(event: KeyboardEvent<HTMLDialogElement>) {
    if (event.key === 'Escape') {
      // Native close stays blocked for every Escape. A request is made once per key press:
      // not for auto-repeat of a held key, not while an input method is composing, and not
      // when content inside the overlay already handled the key.
      const consumed = event.defaultPrevented || event.repeat || event.nativeEvent.isComposing;
      event.preventDefault(); event.stopPropagation();
      if (!consumed) onDismiss('escape');
      return;
    }
    if (event.key !== 'Tab') return;
    const dialog = event.currentTarget;
    const stops = [...dialog.querySelectorAll<HTMLElement>('button, a[href], input, select, textarea, summary, [tabindex], [contenteditable]')]
      .filter(node => availableFocusTarget(node) && (node.tabIndex >= 0 ||
        // A standalone editing host is a native Tab stop even when its tabIndex getter returns -1.
        (!node.hasAttribute('tabindex') && node.isContentEditable && !node.parentElement?.isContentEditable)));
    const index = stops.indexOf(dialog.ownerDocument.activeElement as HTMLElement);
    if (!stops.length) { event.preventDefault(); focusTarget(headingRef.current); }
    else if (event.shiftKey && index <= 0) { event.preventDefault(); focusTarget(stops[stops.length - 1]); }
    else if (!event.shiftKey && (index < 0 || index === stops.length - 1)) { event.preventDefault(); focusTarget(stops[0]); }
  }

  return <dialog ref={dialogRef} className={`ot-modal ot-modal--${kind}`} aria-labelledby={titleId}
    onCancel={event => event.preventDefault()} onKeyDown={handleKey}>
    <h2 ref={headingRef} id={titleId} tabIndex={-1} className="ot-modal-title">{title}</h2>
    <div ref={bodyRef} className="ot-modal-body" data-overflow={overflow === 'block' ? 'block' : undefined}
      {...(overflow !== 'none' ? { tabIndex: 0, role: 'group', 'aria-labelledby': titleId } : {})}>{children}</div>
    <div className="ot-modal-actions"><Button ref={closeRef} variant="secondary" label={kind === 'dialog' ? 'Close dialog' : 'Close details'}
      onPress={() => onDismiss('close-button')} />{actions}</div>
  </dialog>;
}

function ModalPresentation({ open, title, children, actions, initialFocus, returnFocus, fallbackFocus, onDismiss, kind }:
  ModalPresentationProps & { kind: 'dialog' | 'drawer' }) {
  requireLabel(title);
  if (!initialFocus || (initialFocus.kind !== 'heading' && initialFocus.kind !== 'close-button' &&
    (initialFocus.kind !== 'element' || !initialFocus.ref))) throw new Error('An explicit initial focus policy is required.');
  const titleId = useId();
  const resolvers = useRef({ returnFocus, fallbackFocus });
  useLayoutEffect(() => { resolvers.current = { returnFocus, fallbackFocus }; });
  return open ? <ModalContent kind={kind} titleId={titleId} title={title} initialFocus={initialFocus}
    onDismiss={onDismiss} actions={actions} resolvers={resolvers}>{children}</ModalContent> : null;
}

export function Dialog(props: ModalPresentationProps) { return <ModalPresentation {...props} kind="dialog" />; }
export function Drawer(props: ModalPresentationProps) { return <ModalPresentation {...props} kind="drawer" />; }
