import { escapeHtml } from "./escape.ts";
import { tokenize } from "./lexer.ts";

/** Looks up a dotted path like "user.name" in data; missing values render as "". */
export function lookup(data: unknown, path: string): unknown {
  let value: unknown = data;
  for (const key of path.split(".")) {
    if (value === null || value === undefined) return undefined;
    value = (value as Record<string, unknown>)[key];
  }
  return value;
}

function show(value: unknown): string {
  return value === null || value === undefined ? "" : String(value);
}

/** Renders {{ path }} variables (HTML-escaped). */
export function render(template: string, data: Record<string, unknown>): string {
  return tokenize(template)
    .map((t) => (t.kind === "text" ? t.value : escapeHtml(show(lookup(data, t.path)))))
    .join("");
}
