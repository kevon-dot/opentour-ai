'use client';

import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { requireLabel } from '../tokens/require-label';
import '../../../styles/product-tokens.css';
import './single-choice-select.css';

/** Presentation inputs only. No prop represents a lookup, an authorization decision or a receipt. */
export interface SingleChoiceOption {
  /** Stable identity handed back to the caller. Unique in the list and never rendered. */
  value: string;
  /** Visible title of the choice. */
  label: string;
  /** Supporting text shown after the option's state on its second line. */
  detail?: string;
  /** A disabled choice stays listed and readable but can never be highlighted or requested. */
  disabled?: boolean;
  /** Visible reason shown in place of `detail` while the option is disabled. */
  disabledReason?: string;
}

export interface SingleChoiceSelectProps {
  /** Visible name of the control. */
  label: string;
  /** The finite list supplied by the caller, in display order. It is never filtered or fetched here. */
  options: readonly SingleChoiceOption[];
  /**
   * Controlled current value; `null` is no choice. The component never stores, substitutes or clears
   * it. A value that names no supplied option is shown as no choice.
   */
  value: string | null;
  /** A requested choice: only ever an enabled option whose value differs from `value`. */
  onChange: (value: string) => void;
  /** Shown while there is no choice. Omitted, the control shows the design's generic "Select a value". */
  placeholder?: string;
  /**
   * Decoration only: whether the design's caret is typed after the placeholder. It defaults to on for
   * the generic placeholder (Figma 12:39 to 12:55) and off for a supplied one (Figma 166:9963). A
   * chosen value is always shown as its label alone (Figma 31:38), whatever this is set to.
   */
  placeholderCaret?: boolean;
  /** Supporting information under the value. */
  helper?: string;
  /** Visible message that puts the control in its error state and replaces `helper`. */
  error?: string;
  disabled?: boolean;
  /** Visible reason that replaces `helper` while the whole control is disabled. */
  disabledReason?: string;
}

/** The words the design types in front of an option's supporting text (Figma 31:44, 31:47, 31:50). */
export const singleChoiceOptionStates = { selected: 'Selected', available: 'Available', disabled: 'Disabled' } as const;
/** The design separates an option's state from its supporting text with a spaced middle dot. */
const stateSeparator = ' · ';
/** The placeholder the design types when the caller names none (Figma 12:41). */
const genericPlaceholder = 'Select a value';
/** Two spaces then U+25BE, as the design types it after that placeholder (Figma 12:41). Decoration only. */
const caret = '  ▾';

export type SingleChoiceMove = 'first' | 'last' | 'next' | 'previous';

/**
 * Index the highlight moves to, or -1 when no option can be chosen. Disabled options are skipped
 * and the ends do not wrap, so a key press never lands on a choice that cannot be requested.
 */
export function moveHighlight(options: readonly SingleChoiceOption[], from: number, move: SingleChoiceMove): number {
  const enabled = options.flatMap((option, index) => option.disabled ? [] : [index]);
  if (enabled.length === 0) return -1;
  const first = enabled[0];
  const last = enabled[enabled.length - 1];
  if (move === 'first') return first;
  if (move === 'last') return last;
  if (from < 0) return move === 'next' ? first : last;
  if (move === 'next') return enabled.find(index => index > from) ?? last;
  return enabled.findLast(index => index < from) ?? first;
}

/** Opening highlights the current choice when it can be chosen, otherwise the first option that can. */
export function initialHighlight(options: readonly SingleChoiceOption[], value: string | null): number {
  const current = options.findIndex(option => option.value === value && !option.disabled);
  return current >= 0 ? current : moveHighlight(options, -1, 'first');
}

/** Second line of an option: its state in words, then the supplied detail or disabled reason. */
export function optionStateText(option: SingleChoiceOption, selected: boolean): string {
  const parts: (string | undefined)[] = option.disabled
    ? [selected ? singleChoiceOptionStates.selected : undefined, singleChoiceOptionStates.disabled, option.disabledReason ?? option.detail]
    : [selected ? singleChoiceOptionStates.selected : singleChoiceOptionStates.available, option.detail];
  return parts.filter(hasText).join(stateSeparator);
}

export interface SingleChoiceKeyResult {
  /** Whether the list is shown after the key. */
  open: boolean;
  /** Highlighted option index after the key, or -1. */
  highlight: number;
  /** Index of the option being requested, or -1. Moving the highlight never sets it. */
  choose: number;
  /** Whether the key was consumed. Tab is observed so the list closes, but is never consumed. */
  handled: boolean;
}

/**
 * The whole keyboard contract as a pure function: what one key does to the list, the highlight and
 * the request. Only Enter and Space on an enabled highlighted option request a value.
 */
export function resolveKey(key: string, open: boolean, highlight: number,
  options: readonly SingleChoiceOption[], value: string | null): SingleChoiceKeyResult {
  const unchanged = { open, highlight: open ? highlight : -1, choose: -1, handled: false };
  if (!open) {
    if (options.length === 0) return unchanged;
    switch (key) {
      case 'Enter': case ' ': case 'ArrowDown': case 'ArrowUp':
        return { open: true, highlight: initialHighlight(options, value), choose: -1, handled: true };
      case 'Home': return { open: true, highlight: moveHighlight(options, -1, 'first'), choose: -1, handled: true };
      case 'End': return { open: true, highlight: moveHighlight(options, -1, 'last'), choose: -1, handled: true };
      default: return unchanged;
    }
  }
  switch (key) {
    case 'ArrowDown': return { open: true, highlight: moveHighlight(options, highlight, 'next'), choose: -1, handled: true };
    case 'ArrowUp': return { open: true, highlight: moveHighlight(options, highlight, 'previous'), choose: -1, handled: true };
    case 'Home': return { open: true, highlight: moveHighlight(options, highlight, 'first'), choose: -1, handled: true };
    case 'End': return { open: true, highlight: moveHighlight(options, highlight, 'last'), choose: -1, handled: true };
    case 'Enter': case ' ': {
      const option = options[highlight];
      return { open: false, highlight: -1, choose: option && !option.disabled ? highlight : -1, handled: true };
    }
    case 'Escape': return { open: false, highlight: -1, choose: -1, handled: true };
    case 'Tab': return { open: false, highlight: -1, choose: -1, handled: false };
    default: return unchanged;
  }
}

function hasText(text: string | undefined): text is string {
  return typeof text === 'string' && text.trim().length > 0;
}

/**
 * Labelled single-choice control over a supplied finite list. The trigger is a native button with
 * the combobox role; DOM focus stays on it while the list is open and the highlighted option is its
 * active descendant. The list opens in flow under the control, as the design draws its rows.
 */
export function SingleChoiceSelect({ label, options, value, onChange, placeholder, placeholderCaret,
  helper, error, disabled = false, disabledReason }: SingleChoiceSelectProps) {
  const emptyText = placeholder ?? genericPlaceholder;
  requireLabel(label, 'select label');
  requireLabel(emptyText, 'select placeholder');
  if (typeof onChange !== 'function') throw new Error('A single-choice select requires a change handler.');
  if (value !== null && typeof value !== 'string') throw new Error('A single-choice select value is text or null.');
  if (!Array.isArray(options)) throw new Error('A single-choice select requires a list of options.');
  const identities = new Set<string>();
  for (const option of options) {
    requireLabel(option.value, 'option value');
    requireLabel(option.label, 'option label');
    if (identities.has(option.value)) throw new Error('Single-choice option values must be unique.');
    identities.add(option.value);
  }

  const id = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const followHighlight = useRef(false);
  const pressedOutside = useRef(false);
  const [open, setOpen] = useState(false);
  // The highlight is remembered by option identity, so a replaced list cannot leave it on another row.
  const [highlighted, setHighlighted] = useState<string | null>(null);
  // Whether the highlighted row draws its ring. It is presentation only: the highlight, the active
  // descendant and what Enter requests are the same either way. A press that opens the list leaves it
  // neutral, as the design draws it; the keyboard, or a pointer over a row, shows where the highlight is.
  const [highlightRing, setHighlightRing] = useState(false);

  const shown = open && !disabled && options.length > 0;
  const highlight = shown ? options.findIndex(option => option.value === highlighted && !option.disabled) : -1;
  const current = options.find(option => option.value === value);
  // The caret belongs to the placeholder's artwork; a chosen value never carries it.
  const decorated = !current && (placeholderCaret ?? (placeholder === undefined));
  const invalid = !disabled && hasText(error);
  const support = disabled && hasText(disabledReason) ? disabledReason : invalid ? error : hasText(helper) ? helper : undefined;
  const labelId = `${id}-label`;
  const supportId = `${id}-support`;
  const listId = `${id}-list`;
  const optionId = (index: number) => `${id}-option-${index}`;
  const identityAt = (index: number) => index >= 0 ? options[index].value : null;

  function request(option: SingleChoiceOption | undefined) {
    if (!option || option.disabled || option.value === value) return;
    onChange(option.value);
  }

  // A list that can no longer be shown is closed, not left to reappear when the control is usable again.
  useEffect(() => { if (open && !shown) setOpen(false); }, [open, shown]);

  // A keyboard move keeps its target visible; pointer movement never scrolls the page.
  useLayoutEffect(() => {
    if (!followHighlight.current) return;
    followHighlight.current = false;
    const row = highlight >= 0 ? list.current?.children[highlight] : undefined;
    if (row && typeof row.scrollIntoView === 'function') row.scrollIntoView({ block: 'nearest' });
  }, [highlight, shown]);

  // A press outside dismisses without a request. The list opens in flow, so collapsing it while the
  // pointer is still down would move the pressed control away before its click; the list therefore
  // closes on the click that completes the press. Focus stays where the press put it, and returns
  // to the trigger when the press would otherwise leave it on the document.
  useEffect(() => {
    if (!shown) return;
    const node = root.current;
    const document = node?.ownerDocument;
    if (!node || !document) return;
    const outside = (event: Event) => !(event.target instanceof Node && node.contains(event.target));
    const press = (event: PointerEvent) => { pressedOutside.current = event.button === 0 && outside(event); };
    const cancel = () => { pressedOutside.current = false; };
    const dismiss = (event: MouseEvent) => {
      pressedOutside.current = false;
      if (!outside(event)) return;
      setOpen(false);
      const active = document.activeElement;
      if (!active || active === document.body || active === document.documentElement) trigger.current?.focus({ preventScroll: true });
    };
    document.addEventListener('pointerdown', press, true);
    document.addEventListener('pointercancel', cancel, true);
    document.addEventListener('click', dismiss, true);
    return () => {
      pressedOutside.current = false;
      document.removeEventListener('pointerdown', press, true);
      document.removeEventListener('pointercancel', cancel, true);
      document.removeEventListener('click', dismiss, true);
    };
  }, [shown]);

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (disabled || event.ctrlKey || event.metaKey || event.nativeEvent.isComposing) return;
    const result = resolveKey(event.key, shown, highlight, options, value);
    if (!result.handled && result.open === shown) return;
    // Consuming Escape also tells an enclosing dialog that this press was already used.
    if (result.handled) event.preventDefault();
    if (result.choose >= 0) request(options[result.choose]);
    followHighlight.current = result.open;
    // Every key that opens the list or moves within it makes the highlighted row visible.
    if (result.open) setHighlightRing(true);
    setHighlighted(identityAt(result.highlight));
    setOpen(result.open);
  }

  return <div ref={root} className={`ot-single-choice${invalid ? ' ot-single-choice--invalid' : ''}${disabled ? ' ot-single-choice--disabled' : ''}`}>
    <div className="ot-single-choice-control">
      <span id={labelId} className="ot-single-choice-label">{label}</span>
      <button ref={trigger} type="button" role="combobox" className="ot-single-choice-trigger" disabled={disabled}
        aria-haspopup="listbox" aria-expanded={shown} aria-controls={listId} aria-labelledby={labelId}
        aria-describedby={support ? supportId : undefined} aria-invalid={invalid || undefined}
        aria-activedescendant={highlight >= 0 ? optionId(highlight) : undefined}
        onKeyDown={handleKeyDown}
        // Enter and Space are handled on key down; the native Space activation on key up is not a second toggle.
        onKeyUp={event => { if (event.key === ' ') event.preventDefault(); }}
        // Focus leaving by keyboard or script closes at once; an outside press closes on its own click.
        onBlur={() => { if (!pressedOutside.current) setOpen(false); }}
        onClick={event => {
          if (disabled) return;
          if (shown) { setOpen(false); return; }
          if (options.length === 0) return;
          // Not every platform focuses a pressed button; the open list always has a focused owner.
          event.currentTarget.focus({ preventScroll: true });
          // The same row is highlighted as a key would highlight; a press just does not ring it (Figma 31:36).
          setHighlightRing(false);
          setHighlighted(identityAt(initialHighlight(options, value)));
          setOpen(true);
        }}>
        <span className="ot-single-choice-value">{current ? current.label : emptyText}</span>
        {decorated && <span aria-hidden="true" className="ot-single-choice-caret">{caret}</span>}
      </button>
      {support && <span id={supportId} className="ot-single-choice-support">{support}</span>}
    </div>
    <div ref={list} role="listbox" id={listId} aria-labelledby={labelId} className="ot-single-choice-list" hidden={!shown}
      // Pressing a row must not take focus from the trigger, which owns the open list.
      onMouseDown={event => event.preventDefault()}>
      {options.map((option, index) => {
        const selected = option.value === value;
        return <div key={option.value} role="option" id={optionId(index)} className="ot-single-choice-option"
          aria-selected={selected} aria-disabled={option.disabled || undefined}
          data-highlighted={index === highlight || undefined}
          data-highlight-ring={(index === highlight && highlightRing) || undefined}
          onPointerMove={event => {
            if (option.disabled || event.pointerType === 'touch' || (highlightRing && highlighted === option.value)) return;
            followHighlight.current = false;
            setHighlighted(option.value);
            setHighlightRing(true);
          }}
          onClick={() => {
            // A disabled choice is blocked: nothing is requested and the list stays open.
            if (option.disabled) return;
            request(option);
            setOpen(false);
            trigger.current?.focus({ preventScroll: true });
          }}>
          <span className="ot-single-choice-option-title">{option.label}</span>
          <span className="ot-single-choice-option-state">{optionStateText(option, selected)}</span>
        </div>;
      })}
    </div>
  </div>;
}
