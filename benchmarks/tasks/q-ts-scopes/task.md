Add scopes to the template engine in this project (TypeScript, run directly by Node with `node --test tests/`, no build step and no dependencies). Keep the existing behaviour and tests, and add at least three tests of your own for the new behaviour.

Today a name inside `{{#each}}` is looked up only on the current element. Change that:
- A plain name like `name` is first looked up on the current element. If it isn't a property there (`!(key in element)`, or the element isn't an object), it is looked up in the enclosing scopes, from the innermost outwards, up to the top-level data. Dotted paths (`address.city`) follow the same rule for their first name.
- `../name` looks `name` up one scope out, skipping the current element. `../../name` goes two scopes out. `../` can be combined with dotted paths (`../user.name`).
- `this`, `this.name` and `@index` keep working as today and always refer to the current element.
- Blocks nest (`each` in `each`, `if` in `each`), and a partial inside an `each` sees the same scopes.

Node runs `.ts` files by stripping the types, so: import with the `.ts` extension, and don't use `enum`, `namespace` or constructor parameter properties.
