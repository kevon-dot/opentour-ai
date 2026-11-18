import type { BreadcrumbProps, LinkProps, LinkSize, LiveRegionPoliteness, LiveRegionProps } from './primitives.contracts';
import '../../../styles/product-tokens.css';
import './primitives.css';

export type { BreadcrumbItem, BreadcrumbProps, LinkProps, LinkSize, LiveRegionPoliteness, LiveRegionProps } from './primitives.contracts';

/** Declared link text styles, smallest first: Figma Small, Small strong and Label. */
export const linkSizes: readonly LinkSize[] = ['small', 'small-strong', 'label'];
export const liveRegionPolitenessLevels: readonly LiveRegionPoliteness[] = ['polite', 'assertive'];
/** The design types the separator into the trail: a solidus with two spaces on each side. */
export const breadcrumbSeparator = '  /  ';

/** One space then U+2192 RIGHTWARDS ARROW, as the design types it after a link label. */
const linkArrow = ' →';
const navigableSchemes: readonly string[] = ['http', 'https', 'mailto'];

/** Visible text is a contract: blank or non-string text is refused, never rendered empty. */
function requireLabel(label: string, name: string): void {
  if (typeof label !== 'string' || label.trim().length === 0) {
    throw new Error(`A visible foundation component ${name} is required.`);
  }
}

/**
 * Reads the scheme the way a URL parser does: tab and line breaks are dropped anywhere and leading
 * control characters or spaces are ignored, so "java\tscript:" cannot pass as a relative path.
 */
function schemeOf(href: string): string | null {
  let cleaned = '';
  for (const character of href) {
    const code = character.charCodeAt(0);
    if (code !== 9 && code !== 10 && code !== 13) cleaned += character;
  }
  let start = 0;
  while (start < cleaned.length && cleaned.charCodeAt(start) <= 32) start += 1;
  const match = /^[a-z][a-z0-9+.-]*(?=:)/i.exec(cleaned.slice(start));
  return match ? match[0].toLowerCase() : null;
}

/** A destination is a relative reference or an http, https or mailto address; nothing executable. */
function requireDestination(href: string, name: string): void {
  if (typeof href !== 'string' || href.trim().length === 0) {
    throw new Error(`A foundation component ${name} destination is required.`);
  }
  const scheme = schemeOf(href);
  if (scheme !== null && !navigableSchemes.includes(scheme)) {
    throw new Error(`A foundation component ${name} destination must be a path or an http, https or mailto address.`);
  }
}

/**
 * Text link exactly as drawn at rest: link colour, no underline and no hover or visited change,
 * because the design shows none. Keyboard focus uses the approved button outline.
 */
export function Link({ label, href, size, current, trailingArrow = false, onPress, ref }: LinkProps) {
  requireLabel(label, 'link label');
  requireDestination(href, 'link');
  if (!linkSizes.includes(size)) throw new Error('A declared link size is required.');
  return <a ref={ref} href={href} aria-current={current ? 'page' : undefined} className={`ot-link ot-link--${size}`} onClick={onPress}>
    {label}{trailingArrow && <span aria-hidden="true">{linkArrow}</span>}
  </a>;
}

/**
 * Navigation landmark holding an ordered trail. Ancestors with a destination are anchors in the
 * trail's own colour; the last item is the current page, marked `aria-current` and never a link.
 */
export function Breadcrumb({ label, items, onItemPress }: BreadcrumbProps) {
  requireLabel(label, 'breadcrumb label');
  if (!Array.isArray(items) || items.length === 0) throw new Error('A breadcrumb requires at least one item.');
  const last = items.length - 1;
  items.forEach((item, index) => {
    requireLabel(item.label, 'breadcrumb item label');
    if (item.href === undefined) return;
    if (index === last) throw new Error('The current breadcrumb item is not a link; omit its destination.');
    requireDestination(item.href, 'breadcrumb item');
  });
  return <nav className="ot-breadcrumb" aria-label={label}>
    <ol className="ot-breadcrumb-list" role="list">
      {items.map((item, index) => <li key={`${index}:${item.label}`} className="ot-breadcrumb-item">
        {index > 0 && <span className="ot-breadcrumb-separator" aria-hidden="true">{breadcrumbSeparator}</span>}
        {index === last ? <span aria-current="page">{item.label}</span>
          : item.href === undefined ? <span>{item.label}</span>
            : <a className="ot-breadcrumb-link" href={item.href}
              onClick={onItemPress && (event => onItemPress(event, item))}>{item.label}</a>}
      </li>)}
    </ol>
  </nav>;
}

/**
 * Visually hidden announcement text. `status` is polite and `alert` is assertive; each role already
 * implies its live setting and whole-text reading, and repeating them as attributes makes some
 * screen readers speak twice. The same message renders the same text node, so a re-render (for
 * example after a poll) changes nothing and is not announced again.
 */
export function LiveRegion({ message, politeness = 'polite' }: LiveRegionProps) {
  if (typeof message !== 'string') throw new Error('A live region message must be text; use an empty string for silence.');
  if (!liveRegionPolitenessLevels.includes(politeness)) throw new Error('A declared live region politeness is required.');
  return <div role={politeness === 'assertive' ? 'alert' : 'status'} className="ot-live-region">{message}</div>;
}
