import type { MouseEvent, Ref } from 'react';

/** Presentation inputs only. No prop represents server authority, a routing decision or a receipt. */

/**
 * The three resting text styles the design draws for a link (Figma nodes 51:459, 5:36 and 85:22).
 * No other link size, and no hover, pressed, visited or disabled appearance, is designed.
 */
export type LinkSize = 'small' | 'small-strong' | 'label';

/** Navigation only: always an anchor with a destination, never a control that performs an action. */
export interface LinkProps {
  label: string;
  /** A path, query, fragment, or an http, https or mailto address. Other schemes are refused. */
  href: string;
  size: LinkSize;
  /** Draws the designed " →" after the label. Decoration only; it is not part of the name. */
  trailingArrow?: boolean;
  /** Observes the native click, for example to route in the client. Navigation stays native unless prevented. */
  onPress?: (event: MouseEvent<HTMLAnchorElement>) => void;
  ref?: Ref<HTMLAnchorElement>;
}

export interface BreadcrumbItem {
  label: string;
  /** Same destination rules as a link. Omitted for an ancestor that is not navigable. */
  href?: string;
}

/**
 * One uniform trail, as drawn (Figma nodes 5:40 and 13:33): the design gives the current item and
 * its ancestors the same colour and weight. The last item is the current page and is never a link.
 */
export interface BreadcrumbProps {
  /** Name of the navigation landmark. */
  label: string;
  /** Ordered from the root to the current page. At least one item. */
  items: readonly BreadcrumbItem[];
  onItemPress?: (event: MouseEvent<HTMLAnchorElement>, item: BreadcrumbItem) => void;
}

/** Every announcement rule in the design is polite. Assertive exists for callers and is never a default. */
export type LiveRegionPoliteness = 'polite' | 'assertive';

/**
 * Non-visual status text. Keep it mounted and change `message` to announce; an empty message is
 * silent. Identical text is not announced again. Never pass protected or private content.
 */
export interface LiveRegionProps {
  message: string;
  politeness?: LiveRegionPoliteness;
}
