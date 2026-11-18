/** Static presentation only; the consumer owns when loading starts and ends. */
export type SkeletonVariant = 'rows' | 'card';

/** One visible polite status and the finite layout from Figma 153:479 / 153:484. */
export interface SkeletonProps {
  variant: SkeletonVariant;
}
