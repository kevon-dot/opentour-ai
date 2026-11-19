import type { SkeletonProps } from './skeleton.contracts';
import '../../../styles/product-tokens.css';
import './skeleton.css';

export type { SkeletonProps, SkeletonVariant } from './skeleton.contracts';

/** Keep this instance mounted while loading; ordinary rerenders do not change its status text. */
export function Skeleton({ variant }: SkeletonProps) {
  if (variant !== 'rows' && variant !== 'card') throw new Error('A declared skeleton variant is required.');
  return <div className={`ot-skeleton ot-skeleton--${variant}`}>
    <p className="ot-skeleton-label" role="status">Loading…</p>
    <div className="ot-skeleton-bar" aria-hidden="true" />
    <div className="ot-skeleton-bar" aria-hidden="true" />
    <div className="ot-skeleton-bar" aria-hidden="true" />
  </div>;
}
