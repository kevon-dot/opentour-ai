import type { StatusIconProps, StatusIconTone } from './status-icon.contracts';
import { IconAlertCircle } from './status-icon-glyphs/IconAlertCircle';
import { IconX } from './status-icon-glyphs/IconX';
import { requireLabel } from './require-label';
import checkArtwork from './status-icon-assets/check.svg';
import '../../../styles/product-tokens.css';
import './status-icon.css';

export type { StatusIconProps, StatusIconTone } from './status-icon.contracts';
export const statusIconTones: readonly StatusIconTone[] = ['success', 'hold', 'failed', 'unknown', 'info'];

/** Native icon + exact text composition, without announcement or authority behavior. */
export function StatusIcon({ tone, label }: StatusIconProps) {
  requireLabel(label);
  if (!statusIconTones.includes(tone)) throw new Error('A declared status icon tone is required.');
  return <span className={`ot-status-icon ot-status-icon--${tone}`}>
    <span className="ot-status-icon-symbol" aria-hidden="true">
      {tone === 'success' ? <span className="ot-status-icon-check">
        <img src={checkArtwork} width="15.3333" height="11.1667" alt="" />
      </span> : tone === 'failed' ? <IconX /> : <IconAlertCircle />}
    </span>
    <span className="ot-status-icon-label">{label}</span>
  </span>;
}
