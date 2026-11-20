import type { MouseEvent, ReactNode, Ref, RefObject } from 'react';

/** Presentation inputs only. No prop represents server authority or an operation receipt. */
export type ButtonVariant = 'primary' | 'secondary' | 'quiet';
export type BadgeTone = 'success' | 'learning' | 'hold' | 'failure' | 'unknown' | 'info';

export interface ButtonProps {
  label: string;
  variant?: ButtonVariant;
  type?: 'button' | 'submit' | 'reset';
  disabled?: boolean;
  busy?: boolean;
  /** Controlled toggle state; omit for a regular action. */
  pressed?: boolean;
  disabledReason?: string;
  onPress?: (event: MouseEvent<HTMLButtonElement>) => void;
  ref?: Ref<HTMLButtonElement>;
}

export interface BadgeProps {
  label: string;
  tone: BadgeTone;
}

/** Colour only. The caller's title states the condition in words; tone never stands in for it. */
export type NoticeTone = 'info' | 'success' | 'hold' | 'failure' | 'unknown';

/** Persistent status text. Both strings are visible and required; neither is markup. */
export interface NoticeProps {
  tone: NoticeTone;
  title: string;
  body: string;
}

export interface TabItem {
  key: string;
  label: string;
  panel: ReactNode;
  disabled?: boolean;
}

/** Manual activation: moving focus never changes the controlled selected key. */
export interface TabsProps {
  label: string;
  items: readonly TabItem[];
  selectedKey: string;
  onSelectionChange: (key: string) => void;
}

export type DismissReason = 'escape' | 'close-button';
export type InitialFocus = { kind: 'heading' } | { kind: 'close-button' } | { kind: 'element'; ref: RefObject<HTMLElement | null> };

/**
 * Resolvers must exclude targets from an obsolete caller context. No captured origin is restored.
 * `children` are laid out by the component: direct children sit on the 16px rhythm with their own
 * block margins removed, a direct `h3` uses section type, and the `ot-modal-meta` class marks
 * 12/18 metadata text. While the content overflows, its region is a named keyboard stop.
 */
export interface ModalPresentationProps {
  open: boolean;
  title: string;
  children: ReactNode;
  actions?: ReactNode;
  initialFocus: InitialFocus;
  returnFocus: () => HTMLElement | null;
  fallbackFocus: () => HTMLElement | null;
  onDismiss: (reason: DismissReason) => void;
}
