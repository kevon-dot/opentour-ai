/** Visible text is a contract: blank or non-string text is refused, never rendered empty. */
export function requireLabel(label: string, name = 'label'): void {
  if (typeof label !== 'string' || label.trim().length === 0) {
    throw new Error(`A visible foundation component ${name} is required.`);
  }
}
