import assert from "node:assert/strict";
import { test } from "node:test";
import { compile, render, TemplateError } from "../src/template.ts";

test("triple braces are not escaped", () => {
  assert.equal(render("{{{ html }}}|{{html}}", { html: "<i>x</i>" }), "<i>x</i>|&lt;i&gt;x&lt;/i&gt;");
});

test("comments render nothing, even across lines", () => {
  assert.equal(render("a{{! note }}b{{!\n multi\n line }}c", {}), "abc");
});

test("if / else and falsy values", () => {
  const t = compile("{{#if v}}T{{else}}F{{/if}}");
  const got = [false, null, undefined, 0, "", [], true, 1, "x", [0], {}].map((v) => t({ v }));
  assert.deepEqual(got, ["F", "F", "F", "F", "F", "F", "T", "T", "T", "T", "T"]);
  assert.equal(render("{{#if v}}yes{{/if}}", { v: 0 }), "");
});

test("each with this, @index and fields", () => {
  const data = { people: [{ name: "Ann" }, { name: "Bo" }] };
  assert.equal(render("{{#each people}}{{@index}}:{{name}};{{/each}}", data), "0:Ann;1:Bo;");
  assert.equal(render("{{#each xs}}[{{this}}]{{/each}}", { xs: [1, "<2>"] }), "[1][&lt;2&gt;]");
});

test("each else for empty or missing arrays", () => {
  const t = compile("{{#each xs}}{{this}}{{else}}none{{/each}}");
  assert.equal(t({ xs: [] }), "none");
  assert.equal(t({}), "none");
  assert.equal(t({ xs: ["a"] }), "a");
});

test("names fall back to outer scopes", () => {
  const data = { shop: "Mo's", items: [{ name: "pen" }, { name: "ink", shop: "own" }] };
  assert.equal(render("{{#each items}}{{name}}@{{shop}} {{/each}}", data), "pen@Mo&#39;s ink@own ");
});

test("../ skips the current element", () => {
  const data = { name: "top", items: [{ name: "a" }] };
  assert.equal(render("{{#each items}}{{name}}<{{../name}}{{/each}}", data), "a<top");
});

test("nested each with ../ and @index", () => {
  const data = { groups: [{ g: "x", items: [1, 2] }, { g: "y", items: [3] }] };
  const t = "{{#each groups}}{{#each items}}{{../g}}{{@index}}={{this}} {{/each}}{{/each}}";
  assert.equal(render(t, data), "x0=1 x1=2 y0=3 ");
  const deep = { label: "L", rows: [{ cells: [{ v: 1 }] }] };
  assert.equal(render("{{#each rows}}{{#each cells}}{{../../label}}{{v}}{{/each}}{{/each}}", deep), "L1");
});

test("if inside each uses the element", () => {
  const data = { todos: [{ t: "a", done: true }, { t: "b", done: false }] };
  assert.equal(render("{{#each todos}}{{#if done}}[x]{{else}}[ ]{{/if}}{{t}} {{/each}}", data), "[x]a [ ]b ");
});

test("this.field and dotted paths inside each", () => {
  const data = { users: [{ name: "Ann", address: { city: "Oslo" } }] };
  assert.equal(render("{{#each users}}{{this.name}} in {{address.city}}{{/each}}", data), "Ann in Oslo");
});

test("partials see the current scope", () => {
  const partials = { item: "<li>{{name}}</li>", list: "<ul>{{#each items}}{{> item}}{{/each}}</ul>" };
  assert.equal(render("{{> list}}", { items: [{ name: "a" }, { name: "b" }] }, { partials }),
    "<ul><li>a</li><li>b</li></ul>");
});

test("compile parses once and can be reused", () => {
  const t = compile("{{#each xs}}{{this}}{{/each}}");
  assert.equal(t({ xs: [1, 2] }), "12");
  assert.equal(t({ xs: [3] }), "3");
});
