import type { CSSProperties, MouseEventHandler } from 'react';
import { requireLabel } from './require-label';
import './icon-button.css';

export interface IconButtonProps {
  label: string;
  /** Decorative monochrome artwork in a square viewBox. Defaults to the design system's X. */
  iconSrc?: string;
  onClick: MouseEventHandler<HTMLButtonElement>;
  disabled?: boolean;
  loading?: boolean;
  describedBy?: string;
  type?: 'button' | 'submit' | 'reset';
}

export function IconButton({ label, iconSrc, onClick, disabled = false, loading = false, describedBy, type = 'button' }: IconButtonProps) {
  requireLabel(label, 'accessible name');
  const iconStyle = iconSrc ? { '--ot-icon-art': 'url(' + JSON.stringify(iconSrc) + ')' } as CSSProperties : undefined;
  return <button className="ot-icon-button" type={type} aria-label={label} aria-describedby={describedBy}
    disabled={disabled && !loading} aria-disabled={loading || undefined} aria-busy={loading || undefined}
    onClick={event => {
      if (loading || disabled) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      onClick(event);
    }}>
    {loading ? <span aria-hidden="true" className="ot-icon-button-busy">…</span>
      : <span aria-hidden="true" className="ot-icon-button-art" style={iconStyle} />}
  </button>;
}
