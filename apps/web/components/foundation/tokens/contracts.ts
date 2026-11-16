import type { MouseEvent, ReactNode, Ref } from 'react';

/** Presentation inputs only. No prop represents server authority or an operation receipt. */
export type ButtonVariant = 'primary' | 'secondary' | 'quiet';
export type BadgeTone = 'success' | 'learning' | 'hold' | 'failure' | 'unknown' | 'info';

export interface ButtonProps {
  label: string;
  variant?: ButtonVariant;
  type?: 'button' | 'submit' | 'reset';
  disabled?: boolean;
  busy?: boolean;
  disabledReason?: string;
  onPress?: (event: MouseEvent<HTMLButtonElement>) => void;
  ref?: Ref<HTMLButtonElement>;
}

export interface BadgeProps {
  label: string;
  tone: BadgeTone;
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
