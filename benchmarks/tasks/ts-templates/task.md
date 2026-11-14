Extend the template engine in this project (TypeScript, run directly by Node with `node --test tests/`, no build step and no dependencies) into a small Handlebars-like language. Keep the existing behaviour and tests, and add tests of your own.

Node runs `.ts` files by stripping the types, so: import with the `.ts` extension, and don't use `enum`, `namespace` or constructor parameter properties. Don't add dependencies.

## API (`src/template.ts`)
- `render(template: string, data: unknown, options?: { partials?: Record<string, string> }): string`
- `compile(template: string): (data: unknown, options?: { partials?: Record<string, string> }) => string`. It parses once, so syntax errors are thrown by `compile` itself, not when the result is called.
- `class TemplateError extends Error` with a `line: number` property: the 1-based line in the template where the problem is. Export it from `src/template.ts`. Every syntax error is a `TemplateError`.

## Language
Tags may have spaces inside the braces: `{{ name }}` is the same as `{{name}}`.
- `{{path}}`: a variable, HTML-escaped as today. Paths are dotted names (`user.name`), `this`, `this.name`, `@index` or `../name`. Missing values render as empty text.
- `{{{path}}}`: the same, but not escaped.
- `{{! any text }}`: a comment. It renders nothing and may span lines.
- `{{#if path}} … {{else}} … {{/if}}`: the `{{else}}` part is optional. Falsy values are `false`, `null`, `undefined`, `0`, `""` and empty arrays. Everything else is truthy, including empty objects.
- `{{#each path}} … {{else}} … {{/each}}`: renders the body once per element of an array. If the value is missing or the array is empty, it renders the optional `{{else}}` part. Inside the body:
  - `this` is the current element and `@index` is its 0-based index.
  - A plain name like `name` is first looked up on the current element. If it isn't a property there (`!(key in element)`, or the element isn't an object), it is looked up in the enclosing scopes, from the innermost outwards, up to the top-level data.
  - `../name` looks `name` up one scope out, skipping the current element. `../../name` goes two scopes out.
  - Blocks nest: `each` in `each`, `if` in `each`, and so on.
- `{{> name}}`: a partial. Render `options.partials[name]` with the current scope, so a partial inside an `each` sees the current element.

## Errors (TemplateError, with the line of the offending tag)
- an unclosed block, e.g. `{{#if x}}` with no `{{/if}}`: the line of the opening tag;
- a closing tag that doesn't match the open block, e.g. `{{#if x}}{{/each}}`, or a closing tag with no open block: the line of the closing tag;
- `{{else}}` outside an `if` or `each` block;
- an unknown block helper, e.g. `{{#with x}}`;
- an unterminated tag, e.g. `{{name` with no `}}`;
- a partial that isn't in `options.partials` (thrown at render time, `line` = the partial tag's line).
