// node --test: the list-editing rules the canvas rides on. Pure
// functions, so no DOM — apply() replays a replacement on a string.
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { continueList, indentLines } from "./indent.js";

const apply = (value, e) => value.slice(0, e.from) + e.insert + value.slice(e.to);

test("Tab indents the caret's line and keeps the caret on its text", () => {
  const v = "- [ ] a\n- [ ] b";
  const e = indentLines(v, 10, 10);            // caret after "- "
  assert.equal(apply(v, e), "- [ ] a\n  - [ ] b");
  assert.equal(e.selStart, 12);
  assert.equal(e.selEnd, 12);
});

test("Tab indents every selected line as a block", () => {
  const v = "- [ ] a\n- [ ] b\n- [ ] c";
  const e = indentLines(v, 8, 17);             // "- [ ] b\n- [ ] c" start..into c
  assert.equal(apply(v, e), "- [ ] a\n  - [ ] b\n  - [ ] c");
  // the selection still covers the same text, now shifted by the indents
  assert.equal(apply(v, e).slice(e.selStart, e.selEnd), "- [ ] b\n  -");
});

test("a selection ending at a line start leaves that line alone", () => {
  const v = "a\nb\nc";
  const e = indentLines(v, 0, 2);              // "a\n"
  assert.equal(apply(v, e), "  a\nb\nc");
});

test("Shift-Tab strips one level: two spaces, a tab, or a lone space", () => {
  assert.equal(apply("  - x", indentLines("  - x", 4, 4, true)), "- x");
  assert.equal(apply("\t- x", indentLines("\t- x", 3, 3, true)), "- x");
  assert.equal(apply(" - x", indentLines(" - x", 3, 3, true)), "- x");
  assert.equal(indentLines("- x", 1, 1, true), null);
});

test("outdenting with the caret inside the stripped indent clamps to the line start", () => {
  const e = indentLines("  - x", 1, 1, true);
  assert.equal(e.selStart, 0);
});

test("Enter continues a task line at the same indent with an open box", () => {
  const v = "  - [x] done";
  const e = continueList(v, v.length, v.length);
  assert.equal(apply(v, e), "  - [x] done\n  - [ ] ");
  assert.equal(e.selStart, apply(v, e).length);
});

test("Enter mid-item carries the tail onto the new item", () => {
  const v = "- one two";
  const e = continueList(v, 5, 5);
  assert.equal(apply(v, e), "- one\n-  two");
  assert.equal(e.selStart, 8);
});

test("Enter counts a numbered list up", () => {
  const v = "3. c";
  assert.equal(apply(v, continueList(v, 4, 4)), "3. c\n4. ");
});

test("Enter on an empty top-level item ends the list", () => {
  const v = "- a\n- ";
  const e = continueList(v, 6, 6);
  assert.equal(apply(v, e), "- a\n");
  assert.equal(e.selStart, 4);
});

test("Enter on an empty nested item outdents one level", () => {
  const v = "- a\n  - [ ] ";
  const e = continueList(v, v.length, v.length);
  assert.equal(apply(v, e), "- a\n- [ ] ");
});

test("Enter on prose is the browser's", () => {
  assert.equal(continueList("just words", 4, 4), null);
  assert.equal(continueList("- a", 1, 1), null);         // caret inside the marker
  assert.equal(continueList("- a\n- b", 0, 3), null);     // a selection
});

test("Enter on an indented plain line keeps the indent", () => {
  const v = "    note";
  assert.equal(apply(v, continueList(v, 8, 8)), "    note\n    ");
});
