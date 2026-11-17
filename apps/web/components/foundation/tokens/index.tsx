'use client';

import { useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import type { BadgeProps, ButtonProps, TabsProps } from './contracts';
import { requireLabel } from './require-label';
import '../../../styles/product-tokens.css';

export type { BadgeProps, BadgeTone, ButtonProps, ButtonVariant, NoticeProps, NoticeTone, TabItem, TabsProps } from './contracts';
export { Notice, noticeTones } from './notice';

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
