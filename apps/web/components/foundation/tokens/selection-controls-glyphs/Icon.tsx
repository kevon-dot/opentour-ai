import type { ReactNode } from 'react';

/** Local dependency-free adapter for the Figma SDS Icon primitive. Only the mapped 16px size is exposed. */
export interface IconProps { children?: ReactNode }
export function Icon({ children }: IconProps) {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"
    className="ot-checkbox-glyph" aria-hidden="true" focusable="false">{children}</svg>;
}
