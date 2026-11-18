'use client';

import { cloneElement, isValidElement, useId, useLayoutEffect, useRef, useState } from 'react';
import type { HTMLAttributes, ReactElement, Ref } from 'react';
import { requireLabel } from './require-label';
import '../../../styles/product-tokens.css';
import './tooltip.css';

/** Preserve the exact value while offering readable breaks in long fractional evidence. */
export function TooltipText({ text }: { text: string }) {
  return text.split(/([+-]?\d+\.\d{7,})/g).map((part, index) => {
    if (!/^[+-]?\d+\.\d{7,}$/.test(part)) return part;
    const [integer, fraction] = part.split('.');
    const first = fraction.length % 3 || 3;
    const groups = [integer + '.' + fraction.slice(0, first), ...fraction.slice(first).match(/.{1,3}/g)!];
    return <span className="ot-tooltip-fraction" key={index}>{groups.map((group, groupIndex) => <span key={groupIndex}>{group}</span>)}</span>;
  });
}

type TriggerProps = HTMLAttributes<HTMLElement> & { ref?: Ref<HTMLElement> };
export interface TooltipProps {
  /** Supplemental plain text only. Required information must also be available outside help. */
  description: string | readonly string[];
  appearance?: 'help' | 'evidence';
  /** One native focusable element; its name, handlers, ref and existing descriptions are retained. */
  children: ReactElement<TriggerProps>;
}

type Bounds = { left: number; right: number; top: number; bottom: number };

/** Viewport geometry only; no app state, requests, timers or inferred domain behavior. */
export function placeTooltip(trigger: Bounds, width: number, height: number, viewport: { width: number; height: number }, rtl = false) {
  const margin = 16;
  const gap = 16;
  const below = Math.max(0, viewport.height - margin - trigger.bottom - gap);
  const above = Math.max(0, trigger.top - margin - gap);
  const side = height <= below || below >= above ? 'bottom' : 'top';
  const available = side === 'bottom' ? below : above;
  const maxHeight = Math.max(1, available);
  const shownHeight = Math.min(height, maxHeight);
  const left = Math.max(margin, Math.min(rtl ? trigger.right - width : trigger.left, viewport.width - margin - width));
  const top = Math.max(margin, Math.min(side === 'bottom' ? trigger.bottom + gap : trigger.top - gap - shownHeight, viewport.height - margin - shownHeight));
  return { left, top, maxHeight, side } as const;
}

/** A manual top-layer popover avoids clipping and stays above a containing modal without focus transfer. */
export function Tooltip({ description, appearance = 'help', children }: TooltipProps) {
  const paragraphs = typeof description === 'string' ? [description] : description;
  if (!Array.isArray(paragraphs) || paragraphs.length === 0) throw new Error('Tooltip requires supplemental text.');
  for (const text of paragraphs) requireLabel(text, 'tooltip description');
  if (appearance !== 'help' && appearance !== 'evidence') throw new Error('A declared tooltip appearance is required.');
  if (!isValidElement(children) || typeof children.type !== 'string') throw new Error('Tooltip requires one native focusable trigger.');
  const id = useId();
  const trigger = useRef<HTMLElement | null>(null);
  const layer = useRef<HTMLDivElement | null>(null);
  const body = useRef<HTMLDivElement | null>(null);
  const engagement = useRef({ focused: false, trigger: false, popup: false, dismissed: false });
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0, maxHeight: 10000, side: 'bottom' as 'bottom' | 'top', bridgeLeft: 0, bridgeWidth: 0 });
  const original = children.props;
  const sync = () => {
    const current = engagement.current;
    const engaged = current.focused || current.trigger || current.popup;
    if (!engaged) current.dismissed = false;
    setOpen(engaged && !current.dismissed);
  };

  useLayoutEffect(() => {
    if (!open || !layer.current || !body.current || !trigger.current) return;
    const popup = layer.current;
    popup.showPopover();
    const measure = () => {
      if (!trigger.current || !body.current) return;
      const anchor = trigger.current.getBoundingClientRect();
      const box = body.current;
      const width = box.getBoundingClientRect().width;
      const border = box.offsetHeight - box.clientHeight;
      const next = placeTooltip(anchor, width, box.scrollHeight + border,
        { width: document.documentElement.clientWidth, height: window.innerHeight }, getComputedStyle(trigger.current).direction === 'rtl');
      const bridgeLeft = Math.min(anchor.left, next.left) - next.left;
      const bridgeWidth = Math.max(anchor.right, next.left + width) - Math.min(anchor.left, next.left);
      setPosition(current => {
        const result = { ...next, bridgeLeft, bridgeWidth };
        return Object.keys(result).every(key => current[key as keyof typeof current] === result[key as keyof typeof result]) ? current : result;
      });
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      engagement.current.dismissed = true;
      engagement.current.popup = false;
      if (!engagement.current.focused && !engagement.current.trigger) engagement.current.dismissed = false;
      setOpen(false);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(trigger.current);
    observer.observe(body.current);
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    document.addEventListener('keydown', escape, true);
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
      document.removeEventListener('keydown', escape, true);
      if (popup.matches(':popover-open')) popup.hidePopover();
    };
  }, [open, appearance, paragraphs.join('\n')]);

  const describedBy = [original['aria-describedby'], id].filter(Boolean).join(' ');
  return <>
    {cloneElement(children, {
      'aria-describedby': describedBy,
      ref: node => {
        trigger.current = node;
        if (typeof original.ref === 'function') return original.ref(node);
        if (original.ref) original.ref.current = node;
      },
      onFocus: event => { original.onFocus?.(event); engagement.current.focused = true; sync(); },
      onBlur: event => { original.onBlur?.(event); engagement.current.focused = false; sync(); },
      onPointerEnter: event => { original.onPointerEnter?.(event); if (event.pointerType === 'touch') return; engagement.current.trigger = true; sync(); },
      onPointerLeave: event => {
        original.onPointerLeave?.(event);
        engagement.current.trigger = false;
        engagement.current.popup = event.relatedTarget instanceof Node && Boolean(layer.current?.contains(event.relatedTarget));
        sync();
      },
    })}
    <div ref={layer} popover="manual" hidden={!open}
      className={`ot-tooltip-layer ot-tooltip-layer--${appearance} ot-tooltip-layer--${position.side}`}
      style={{ left: position.left, top: position.top }}
      onPointerEnter={() => { engagement.current.popup = true; sync(); }}
      onPointerLeave={event => {
        engagement.current.popup = false;
        engagement.current.trigger = event.relatedTarget instanceof Node && Boolean(trigger.current?.contains(event.relatedTarget));
        sync();
      }}>
      <div aria-hidden="true" className="ot-tooltip-bridge" style={{ left: position.bridgeLeft, width: position.bridgeWidth }} />
      <div ref={body} id={id} role="tooltip" aria-label={paragraphs.join(' ')} className="ot-tooltip" style={{ maxHeight: position.maxHeight }}>
        {paragraphs.map((text, index) => <p key={index}><TooltipText text={text} /></p>)}
      </div>
    </div>
  </>;
}
