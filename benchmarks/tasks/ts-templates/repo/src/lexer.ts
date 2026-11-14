export type Token =
  | { kind: "text"; value: string; line: number }
  | { kind: "var"; path: string; line: number };

/** Splits a template into text and {{ variable }} tokens. */
export function tokenize(template: string): Token[] {
  const tokens: Token[] = [];
  const re = /\{\{\s*([\w.]+)\s*\}\}/g;
  let last = 0;
  let line = 1;
  for (const m of template.matchAll(re)) {
    const start = m.index ?? 0;
    const text = template.slice(last, start);
    if (text) tokens.push({ kind: "text", value: text, line });
    line += countLines(text);
    tokens.push({ kind: "var", path: m[1], line });
    last = start + m[0].length;
  }
  const rest = template.slice(last);
  if (rest) tokens.push({ kind: "text", value: rest, line });
  return tokens;
}

function countLines(text: string): number {
  return (text.match(/\n/g) ?? []).length;
}
