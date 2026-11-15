import assert from "node:assert/strict";
import { test } from "node:test";
import { render } from "../src/template.ts";

test("names fall back to outer scopes", () => {
  const data = { shop: "Mo's", items: [{ name: "pen" }, { name: "ink", shop: "own" }] };
  assert.equal(render("{{#each items}}{{name}}@{{shop}} {{/each}}", data), "pen@Mo&#39;s ink@own ");
});

test("fallback goes through every enclosing scope", () => {
  const data = { site: "S", groups: [{ g: "x", rows: [{ v: 1 }] }] };
  assert.equal(render("{{#each groups}}{{#each rows}}{{site}}{{g}}{{v}}{{/each}}{{/each}}", data), "Sx1");
});

test("dotted paths fall back by their first name", () => {
  const data = { user: { name: "Ann" }, xs: [1] };
  assert.equal(render("{{#each xs}}{{user.name}}{{/each}}", data), "Ann");
});

test("../ skips the current element", () => {
  const data = { name: "top", items: [{ name: "a" }] };
  assert.equal(render("{{#each items}}{{name}}<{{../name}}{{/each}}", data), "a<top");
});

test("nested each with ../ and @index", () => {
  const data = { groups: [{ g: "x", items: [1, 2] }, { g: "y", items: [3] }] };
  const t = "{{#each groups}}{{#each items}}{{../g}}{{@index}}={{this}} {{/each}}{{/each}}";
  assert.equal(render(t, data), "x0=1 x1=2 y0=3 ");
});

test("../../ goes two scopes out, and ../ works with dotted paths", () => {
  const deep = { label: "L", user: { name: "U" }, rows: [{ cells: [{ v: 1 }] }] };
  assert.equal(render("{{#each rows}}{{#each cells}}{{../../label}}{{v}}{{/each}}{{/each}}", deep), "L1");
  assert.equal(render("{{#each rows}}{{../user.name}}{{/each}}", deep), "U");
});

test("a property that exists but is falsy is not looked up outside", () => {
  const data = { n: 5, items: [{ n: 0 }, { n: null }] };
  assert.equal(render("{{#each items}}[{{n}}]{{/each}}", data), "[0][]");
});

test("if inside each uses the element", () => {
  const data = { todos: [{ t: "a", done: true }, { t: "b", done: false }] };
  assert.equal(render("{{#each todos}}{{#if done}}[x]{{else}}[ ]{{/if}}{{t}} {{/each}}", data), "[x]a [ ]b ");
});

test("this.field stays on the current element", () => {
  const data = { name: "top", users: [{ address: { city: "Oslo" } }] };
  assert.equal(render("{{#each users}}[{{this.name}}] in {{address.city}}{{/each}}", data), "[] in Oslo");
});

test("partials inside each see the outer scopes too", () => {
  const partials = { item: "<li>{{name}}/{{owner}}</li>" };
  assert.equal(render("{{#each items}}{{> item}}{{/each}}", { owner: "Bo", items: [{ name: "a" }] }, { partials }),
    "<li>a/Bo</li>");
});
