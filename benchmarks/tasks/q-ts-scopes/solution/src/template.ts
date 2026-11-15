import { escapeHtml } from "./escape.ts";
import { TemplateError, tokenize, type Token } from "./lexer.ts";

export { TemplateError };

type Options = { partials?: Record<string, string> };
type Node =
  | { kind: "text"; value: string }
  | { kind: "var"; path: string; raw: boolean }
  | { kind: "partial"; name: string; line: number }
  | { kind: "block"; name: string; arg: string; body: Node[]; other: Node[]; line: number };
type Scope = { value: unknown; index?: number; parent?: Scope };

const BLOCKS = new Set(["if", "each"]);

function parse(tokens: Token[]): Node[] {
  const root: Node[] = [];
  const stack: { node: Extract<Node, { kind: "block" }>; target: Node[] }[] = [];
  let target = root;
  for (const t of tokens) {
    if (t.kind === "text") target.push({ kind: "text", value: t.value });
    else if (t.kind === "var") target.push({ kind: "var", path: t.path, raw: t.raw });
    else if (t.kind === "partial") target.push({ kind: "partial", name: t.name, line: t.line });
    else if (t.kind === "open") {
      if (!BLOCKS.has(t.name)) throw new TemplateError(`unknown block helper '${t.name}'`, t.line);
      const node = { kind: "block" as const, name: t.name, arg: t.arg, body: [], other: [], line: t.line };
      target.push(node);
      stack.push({ node, target });
      target = node.body;
    } else if (t.kind === "else") {
      const top = stack[stack.length - 1];
      if (!top || target !== top.node.body) throw new TemplateError("{{else}} outside a block", t.line);
      target = top.node.other;
    } else {
      const top = stack.pop();
      if (!top || top.node.name !== t.name) throw new TemplateError(`unexpected {{/${t.name}}}`, t.line);
      target = top.target;
    }
  }
  const open = stack.pop();
  if (open) throw new TemplateError(`unclosed {{#${open.node.name}}}`, open.node.line);
  return root;
}

function resolve(scope: Scope, path: string): unknown {
  if (path === "@index") return scope.index;
  let s: Scope | undefined = scope;
  let rest = path;
  while (rest.startsWith("../")) {
    s = s?.parent;
    rest = rest.slice(3);
  }
  if (!s) return undefined;
  if (rest === "this") return s.value;
  if (rest.startsWith("this.")) return dig(s.value, rest.slice(5).split("."));
  const [first, ...more] = rest.split(".");
  for (let c: Scope | undefined = s; c; c = c.parent) {
    if (c.value !== null && typeof c.value === "object" && first in (c.value as object)) {
      return dig((c.value as Record<string, unknown>)[first], more);
    }
  }
  return undefined;
}

function dig(value: unknown, keys: string[]): unknown {
  for (const k of keys) {
    if (value === null || value === undefined) return undefined;
    value = (value as Record<string, unknown>)[k];
  }
  return value;
}

function truthy(v: unknown): boolean {
  return Array.isArray(v) ? v.length > 0 : Boolean(v);
}

function show(v: unknown): string {
  return v === null || v === undefined ? "" : String(v);
}

function run(nodes: Node[], scope: Scope, options: Options): string {
  return nodes.map((n) => one(n, scope, options)).join("");
}

function one(n: Node, scope: Scope, options: Options): string {
  if (n.kind === "text") return n.value;
  if (n.kind === "var") {
    const text = show(resolve(scope, n.path));
    return n.raw ? text : escapeHtml(text);
  }
  if (n.kind === "partial") {
    const src = options.partials?.[n.name];
    if (src === undefined) throw new TemplateError(`unknown partial '${n.name}'`, n.line);
    return run(parse(tokenize(src)), scope, options);
  }
  const value = resolve(scope, n.arg);
  if (n.name === "if") return run(truthy(value) ? n.body : n.other, scope, options);
  if (!Array.isArray(value) || value.length === 0) return run(n.other, scope, options);
  return value.map((item, index) => run(n.body, { value: item, index, parent: scope }, options)).join("");
}

export function lookup(data: unknown, path: string): unknown {
  return dig(data, path.split("."));
}

export function compile(template: string): (data: unknown, options?: Options) => string {
  const nodes = parse(tokenize(template));
  return (data, options = {}) => run(nodes, { value: data }, options);
}

export function render(template: string, data: unknown, options: Options = {}): string {
  return compile(template)(data, options);
}
