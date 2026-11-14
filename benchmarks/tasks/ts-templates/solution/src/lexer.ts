export class TemplateError extends Error {
  line: number;
  constructor(message: string, line: number) {
    super(`${message} (line ${line})`);
    this.name = "TemplateError";
    this.line = line;
  }
}

export type Token =
  | { kind: "text"; value: string; line: number }
  | { kind: "var"; path: string; raw: boolean; line: number }
  | { kind: "open"; name: string; arg: string; line: number }
  | { kind: "else"; line: number }
  | { kind: "close"; name: string; line: number }
  | { kind: "partial"; name: string; line: number };

function lines(text: string): number {
  return (text.match(/\n/g) ?? []).length;
}

export function tokenize(template: string): Token[] {
  const tokens: Token[] = [];
  let pos = 0;
  let line = 1;
  while (pos < template.length) {
    const start = template.indexOf("{{", pos);
    if (start < 0) {
      tokens.push({ kind: "text", value: template.slice(pos), line });
      break;
    }
    if (start > pos) {
      const text = template.slice(pos, start);
      tokens.push({ kind: "text", value: text, line });
      line += lines(text);
    }
    const raw = template.startsWith("{{{", start);
    const close = raw ? "}}}" : "}}";
    const end = template.indexOf(close, start + (raw ? 3 : 2));
    if (end < 0) throw new TemplateError("unterminated tag", line);
    const inner = template.slice(start + (raw ? 3 : 2), end).trim();
    tokens.push(...tag(inner, raw, line));
    line += lines(template.slice(start, end));
    pos = end + close.length;
  }
  return tokens;
}

function tag(inner: string, raw: boolean, line: number): Token[] {
  if (raw) return [{ kind: "var", path: inner, raw: true, line }];
  if (inner.startsWith("!")) return [];
  if (inner === "else") return [{ kind: "else", line }];
  if (inner.startsWith("#")) {
    const [name, ...rest] = inner.slice(1).trim().split(/\s+/);
    return [{ kind: "open", name, arg: rest.join(" "), line }];
  }
  if (inner.startsWith("/")) return [{ kind: "close", name: inner.slice(1).trim(), line }];
  if (inner.startsWith(">")) return [{ kind: "partial", name: inner.slice(1).trim(), line }];
  return [{ kind: "var", path: inner, raw: false, line }];
}
