/* List editing for a plain textarea — the canvas has no editor engine
   (nothing fights the buffer), so Tab, Shift-Tab, and Enter get the
   three behaviors a list needs and nothing else. Pure functions: each
   takes the buffer and selection and returns ONE range replacement
   {from, to, insert, selStart, selEnd}, or null for "let the browser do
   the default". The caller applies it through the textarea's own edit
   path so undo still works. Two spaces per level — what the vault's
   task grammar (tasks.py INDENT) and the editor both write. */

export const INDENT = "  ";

// leading whitespace, an optional list marker (bullet, checkbox, or
// number), the rest
const ITEM_RE = /^(\s*)((?:[-*+] (?:\[.\] )?)|(?:\d+[.)] ))?(.*)$/;

/* The lines a selection touches. A selection ending exactly at a line
   start has not touched that line. */
function lineBounds(value, start, end) {
  const from = value.lastIndexOf("\n", start - 1) + 1;
  const last = end > start && value[end - 1] === "\n" ? end - 1 : end;
  const nl = value.indexOf("\n", last);
  return { from, to: nl === -1 ? value.length : nl };
}

/* Tab / Shift-Tab: indent or outdent every line the selection touches,
   as a whole — line-level, like a list editor, not "insert a tab at
   the caret". Outdenting strips one level: two spaces, a tab, or a
   lone space, whatever the line actually starts with. The selection
   keeps covering the same text. */
export function indentLines(value, start, end, outdent = false) {
  const { from, to } = lineBounds(value, start, end);
  const lines = value.slice(from, to).split("\n");
  let firstDelta = 0;
  let total = 0;
  const out = lines.map((ln, i) => {
    let delta = INDENT.length;
    let next = INDENT + ln;
    if (outdent) {
      const strip = ln.startsWith(INDENT) ? INDENT.length
        : /^[\t ]/.test(ln) ? 1 : 0;
      next = ln.slice(strip);
      delta = -strip;
    }
    if (i === 0) firstDelta = delta;
    total += delta;
    return next;
  });
  const insert = out.join("\n");
  if (insert === value.slice(from, to)) return null;  // nothing to outdent
  const single = start === end;
  const selStart = Math.max(from, start + firstDelta);
  const selEnd = single ? selStart : Math.max(selStart, end + total);
  return { from, to, insert, selStart, selEnd };
}

/* Enter on a list line: continue the list. The new line keeps the
   indent and gets a fresh marker — a checked box becomes an open one,
   a number counts up. Text after the caret moves down onto the new
   item. Enter on an EMPTY item ends the list instead: an indented one
   outdents a level, a top-level one clears its marker. A line that is
   not a list item returns null and Enter behaves normally. */
export function continueList(value, start, end) {
  if (start !== end) return null;
  const { from, to } = lineBounds(value, start, start);
  const line = value.slice(from, to);
  const [, ws, marker = "", rest] = ITEM_RE.exec(line);
  if (!marker) {
    // a plain indented line keeps its indent, nothing more
    if (!ws) return null;
    return { from: start, to: start, insert: "\n" + ws,
             selStart: start + 1 + ws.length, selEnd: start + 1 + ws.length };
  }
  if (!rest.trim() && start >= to) {
    if (ws) {
      const less = ws.startsWith(INDENT) ? ws.slice(INDENT.length) : ws.slice(1);
      const insert = less + marker;
      return { from, to, insert, selStart: from + insert.length, selEnd: from + insert.length };
    }
    return { from, to, insert: "", selStart: from, selEnd: from };
  }
  if (start < from + ws.length + marker.length) return null;  // caret inside the marker
  const num = /^(\d+)([.)] )$/.exec(marker);
  const next = num ? `${Number(num[1]) + 1}${num[2]}`
    : /^\[.\] $/.test(marker.slice(2)) ? marker.slice(0, 2) + "[ ] "
    : marker;
  const insert = "\n" + ws + next;
  return { from: start, to: start, insert,
           selStart: start + insert.length, selEnd: start + insert.length };
}

/* Apply a replacement through the textarea's own edit path (undoable,
   and it fires the input event React's onChange listens to), falling
   back to setRangeText where execCommand is gone. */
export function applyEdit(el, edit) {
  el.setSelectionRange(edit.from, edit.to);
  let ok = false;
  try {
    ok = document.execCommand("insertText", false, edit.insert);
  } catch {
    ok = false;
  }
  if (!ok || el.value.slice(edit.from, edit.from + edit.insert.length) !== edit.insert) {
    el.setRangeText(edit.insert, edit.from, edit.to, "end");
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
  el.setSelectionRange(edit.selStart, edit.selEnd);
}

/* The keydown handler the canvas installs: returns true when it
   handled the key (and the caller must preventDefault). */
export function listKey(el, e) {
  const { selectionStart: s, selectionEnd: t, value } = el;
  let edit = null;
  if (e.key === "Tab" && !e.metaKey && !e.ctrlKey && !e.altKey) {
    edit = indentLines(value, s, t, e.shiftKey);
    if (!edit) return true;  // an outdent with nothing to strip still eats the Tab
  } else if (e.key === "Enter" && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey) {
    edit = continueList(value, s, t);
    if (!edit) return false;
  } else {
    return false;
  }
  applyEdit(el, edit);
  return true;
}
