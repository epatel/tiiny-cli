import assert from "node:assert/strict";
import { test } from "node:test";
import { compile, render, TemplateError } from "../src/template.ts";

function lineOf(fn: () => unknown): number {
  try {
    fn();
  } catch (e) {
    assert.ok(e instanceof TemplateError, `expected a TemplateError, got ${e}`);
    assert.ok(e instanceof Error);
    return (e as TemplateError).line;
  }
  assert.fail("no error thrown");
}

test("unclosed block: line of the opening tag", () => {
  assert.equal(lineOf(() => compile("a\n\n{{#if x}}\nb")), 3);
});

test("mismatched or stray closing tag: line of the closing tag", () => {
  assert.equal(lineOf(() => compile("{{#if x}}\n{{/each}}")), 2);
  assert.equal(lineOf(() => compile("x\ny\n{{/if}}")), 3);
});

test("else outside a block", () => {
  assert.equal(lineOf(() => compile("a\n{{else}}")), 2);
});

test("unknown block helper", () => {
  assert.equal(lineOf(() => compile("{{#with x}}{{/with}}")), 1);
});

test("unterminated tag", () => {
  assert.equal(lineOf(() => compile("ok\n{{name")), 2);
});

test("errors are thrown by compile, not later", () => {
  assert.throws(() => compile("{{#each xs}}"), TemplateError);
});

test("lines count across comments and multi-line text", () => {
  assert.equal(lineOf(() => compile("{{! one\ntwo }}\n{{#if a}}{{/each}}")), 3);
});

test("unknown partial at render time", () => {
  const t = compile("x\n{{> nope}}");
  assert.equal(lineOf(() => t({}, { partials: {} })), 2);
  assert.equal(lineOf(() => render("{{> nope}}", {})), 1);
});
