import assert from "node:assert/strict";
import { test } from "node:test";
import { render, TemplateError } from "../src/template.ts";

test("variables are escaped", () => {
  assert.equal(render("Hi {{ name }}!", { name: "<b>Ann</b>" }), "Hi &lt;b&gt;Ann&lt;/b&gt;!");
});

test("dotted paths and missing values", () => {
  assert.equal(render("{{user.name}}/{{user.age}}", { user: { name: "Bo" } }), "Bo/");
});

test("each with index", () => {
  assert.equal(render("{{#each xs}}{{@index}}={{this}} {{/each}}", { xs: ["a", "b"] }), "0=a 1=b ");
});

test("unclosed if", () => {
  assert.throws(() => render("{{#if x}}", {}), TemplateError);
});

test("partials", () => {
  assert.equal(render("{{> p}}", { n: 1 }, { partials: { p: "<{{n}}>" } }), "<1>");
});

test("raw output", () => {
  assert.equal(render("{{{h}}}", { h: "<b>" }), "<b>");
});

test("outer scopes", () => {
  assert.equal(render("{{#each xs}}{{t}}{{/each}}", { t: "T", xs: [1] }), "T");
});

test("../ paths", () => {
  assert.equal(render("{{#each xs}}{{../t}}{{/each}}", { t: "T", xs: [{ t: "x" }] }), "T");
});

test("nested ../../", () => {
  assert.equal(render("{{#each a}}{{#each b}}{{../../t}}{{/each}}{{/each}}", { t: "T", a: [{ b: [1] }] }), "T");
});
