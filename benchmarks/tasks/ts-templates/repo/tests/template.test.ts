import assert from "node:assert/strict";
import { test } from "node:test";
import { render } from "../src/template.ts";

test("variables are escaped", () => {
  assert.equal(render("Hi {{ name }}!", { name: "<b>Ann</b>" }), "Hi &lt;b&gt;Ann&lt;/b&gt;!");
});

test("dotted paths and missing values", () => {
  assert.equal(render("{{user.name}}/{{user.age}}", { user: { name: "Bo" } }), "Bo/");
});
