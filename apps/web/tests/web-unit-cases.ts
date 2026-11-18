import { it } from 'node:test';
import { format } from 'node:util';

type CaseArguments<T> = T extends readonly unknown[] ? [...T] : [T];

/** Registers one native test per row and preserves the suite's parameterized titles. */
export function testCases<const T>(rows: readonly T[]) {
  return (title: string, execute: (...args: CaseArguments<T>) => void | Promise<void>) => {
    for (const [index, row] of rows.entries()) {
      const args = Array.isArray(row) ? row : [row];
      let column = 0;
      const formatted = title.replace(/%[#$%sdifjoOc]/g, token => {
        if (token === '%#') return String(index);
        if (token === '%$') return String(index + 1);
        if (token === '%%') return '%';
        return column < args.length ? format(token, args[column++]) : token;
      });
      const label = args[0] && typeof args[0] === 'object'
        ? formatted.replace(/\$([A-Za-z_][\w.]*)/g, (token, path: string) => {
          let property: unknown = args[0];
          for (const segment of path.split('.')) {
            if (!property || typeof property !== 'object' || !Object.hasOwn(property, segment)) return token;
            property = (property as Record<string, unknown>)[segment];
          }
          return format('%s', property);
        }) : formatted;
      it(label, () => execute(...args as CaseArguments<T>));
    }
  };
}
