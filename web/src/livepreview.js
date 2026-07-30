import { RangeSet, StateEffect, StateField } from "@codemirror/state";
import { Decoration, EditorView, ViewPlugin, WidgetType } from "@codemirror/view";
import { postJSON } from "./api.js";

/* Arriving from the dashboard: the row you clicked gets marked so you
   land on it instead of re-finding it. This IS the selection, editor
   side — the dashboard row stays lit, this line stays marked, and both
   release together (esc, a second click, closing the note); it never
   fades on its own. The caret is deliberately NOT moved: moving it
   would reveal that line's raw markdown, and you came here to read the
   thing, not edit its syntax. */
export const setTarget = StateEffect.define();

export const targetField = StateField.define({
  create: () => Decoration.none,
  update(deco, tr) {
    deco = deco.map(tr.changes);
    for (const e of tr.effects) {
      if (e.is(setTarget))
        deco = e.value == null
          ? Decoration.none
          : Decoration.set([Decoration.line({ class: "cm-target" }).range(e.value)]);
    }
    return deco;
  },
  provide: (f) => EditorView.decorations.from(f),
});

/* Live preview for THIS vault's grammar.
   ---------------------------------------------------------------
   The document is the file. Every rule below is a *decoration* — a
   view layer painted over unchanged text — so nothing here can alter
   a byte. That is the whole reason we're on CodeMirror: there is no
   serialize step to be lossy.

   The live-preview rule: syntax is hidden until your cursor is on the
   line, then it reveals so you can edit it. Nothing is ever hidden
   that you can't get back by moving the caret onto it.

   The patterns mirror tezcat_vault/tasks.py and ids.py exactly — this
   vault's grammar is defined by regexes, so the editor reads it with
   the same ones rather than a generic CommonMark parse. */

const TASK_RE = /^(\s*)([-*]) \[(.)\]\s+/;
const BULLET_RE = /^(\s*)([-*+]) (?!\[.\] )/;
// ⌛ parses, ⏳ is what we write — same pair tasks.py accepts.
const DATE_RE = /(📅|✅|➕|⏳|⌛)\s*(\d{4}-\d{2}-\d{2})/g;
const SCHEDULED_RE = /[⏳⌛]\s*(\d{4}-\d{2}-\d{2})/;
const BLOCK_REF_RE = /\s+\^([A-Za-z0-9][A-Za-z0-9_-]*)\s*$/;
// /u is load-bearing, not pedantry: without it this class decomposes
// into UTF-16 surrogate halves, and 🔺🔼🔽 share their high surrogate
// (D83D) with most emoji — 👤 included — so the priority decoration
// could match HALF of an owner glyph and corrupt the line's rendering.
const PRIORITY_RE = /[🔺⏫🔼🔽⏬]/gu;
const WIKILINK_RE = /\[\[([^\]]+)\]\]/g;
const BOLD_RE = /\*\*([^*]+)\*\*/g;
const HEADING_RE = /^(#{1,6})\s+/;
// A callout title: `> [!NOTE]- Verbatim — 14:32` (the space after > is
// optional — hand-typed `>[!NOTE]` is the same callout). `-` marks it
// collapsed by default; the body is every following quoted line. This
// is the block the record pipeline folds a dump's verbatim into, so a
// pasted transcript must read as one collapsed row, not a wall.
const CALLOUT_RE = /^>\s*\[!(\w+)\]([-+]?)\s?(.*)$/;
const QUOTE_PREFIX_RE = /^\s*>\s?/;

/* The checkbox: a real affordance over plain text. Clicking dispatches
   a one-character replacement of the mark inside the brackets — the
   file still says `- [x]`, we just drew a box on top of it. This is the
   pattern every "block affordance" in a text model follows. */
class TaskBox extends WidgetType {
  constructor(checked, markPos) {
    super();
    this.checked = checked;
    this.markPos = markPos;
  }
  eq(other) {
    return other.checked === this.checked && other.markPos === this.markPos;
  }
  toDOM(view) {
    const box = document.createElement("span");
    box.className = "cm-taskbox" + (this.checked ? " on" : "");
    box.setAttribute("role", "checkbox");
    box.setAttribute("aria-checked", String(this.checked));
    box.title = this.checked ? "Reopen" : "Complete";
    box.onmousedown = (e) => {
      e.preventDefault();
      // "affordance": the editor saves this on a clean buffer — a click
      // on a checkbox is the dashboard's done-verb, not a keystroke.
      view.dispatch({
        changes: { from: this.markPos, to: this.markPos + 1,
                   insert: this.checked ? " " : "x" },
        userEvent: "affordance.task",
      });
    };
    return box;
  }
  ignoreEvent() {
    return false;   // the click is ours, not the editor's
  }
}

/* The two dates the plan chips write, pushed in from the dashboard
   (which already computes them for its own rows) rather than derived
   here — "this week" means the week's Sunday anchor, and two surfaces
   disagreeing about which Sunday that is would be a real bug. Carried
   as an effect, like setTarget, because the editor is built once and
   these change with the calendar. */
export const setPlanDates = StateEffect.define();

export const planDatesField = StateField.define({
  create: () => ({ today: "", week: "" }),
  update(dates, tr) {
    for (const e of tr.effects) if (e.is(setPlanDates)) return e.value;
    return dates;
  },
});

/* Plan chips on an open task: the same ⏳ verb the dashboard's rows
   offer, on the surface where you're actually reading the task.

   The rewrite itself comes from the server (/api/task/line), which
   returns the new line and writes nothing. Two shapes were rejected:
   /api/task/plan would change the file under a buffer that may hold
   unsaved edits, forcing the stale-hash reload that discards them; and
   a JS reimplementation would put this vault's task grammar in two
   languages, with the engine re-reading whatever we wrote using the
   Python regexes. Like the checkbox, what lands here is a DOCUMENT
   edit — ⌘S is still the write, through the same gated /api/note. */
class PlanChips extends WidgetType {
  constructor(lineFrom, planned, dates) {
    super();
    this.lineFrom = lineFrom;
    this.planned = planned;      // the ⏳ date already on the line, or ""
    this.dates = dates;
  }
  eq(other) {
    return other.lineFrom === this.lineFrom && other.planned === this.planned
      && other.dates.today === this.dates.today && other.dates.week === this.dates.week;
  }
  toDOM(view) {
    const wrap = document.createElement("span");
    wrap.className = "cm-plan";
    const chip = (label, when) => {
      const on = Boolean(when) && this.planned === when;
      const b = document.createElement("button");
      b.className = "cm-plan-btn" + (on ? " on" : "");
      b.textContent = label;
      b.title = on ? `Planned ${when} — click to clear` : `Plan for ${when} (⏳)`;
      b.onmousedown = async (e) => {
        e.preventDefault();          // never steal the caret from the line
        const before = view.state.doc.lineAt(this.lineFrom).text;
        let next;
        try {
          ({ line: next } = await postJSON("/api/task/line",
                                           { line: before, when: on ? "" : when }));
        } catch {
          return;                    // server unreachable: the line stands
        }
        // The round trip is localhost-fast but not instant, and the
        // document is live under it. Re-find the line and apply only if it
        // still says what we sent — a keystroke that landed meanwhile owns
        // the line, and we must not paste a stale rewrite over it.
        const now = view.state.doc.lineAt(this.lineFrom);
        if (now.text === before && next !== before)
          // "affordance": on a clean buffer the editor saves this — the
          // chip is the same plan verb the dashboard applies instantly,
          // and a silent buffer edit that still needs ⌘S reads as "the
          // move didn't happen" from the Today column.
          view.dispatch({ changes: { from: now.from, to: now.to, insert: next },
                          userEvent: "affordance.plan" });
      };
      wrap.appendChild(b);
    };
    chip("→ today", this.dates.today);
    // On a Sunday the week anchor IS today, and two chips doing the
    // identical thing is just noise.
    if (this.dates.week && this.dates.week !== this.dates.today)
      chip("→ this week", this.dates.week);
    return wrap;
  }
  ignoreEvent() {
    return false;   // the click is ours, not the editor's
  }
}

/* Callout folding. The default comes from the marker (`-` starts
   collapsed); clicking the header toggles, and the toggle is remembered
   per block — positions map through edits, so typing above a folded
   verbatim doesn't spring it open. Like every rule here it is a view
   decision: the bytes on disk never change.

   This layer lives in a StateField, not the view plugin, because a
   folded block is a `block: true` replace decoration — those change
   vertical layout, and CodeMirror refuses them from plugins ("block
   decorations may not be specified via plugins"). One field owns the
   toggles AND the decorations built from them; the plugin just skips
   the lines this field has claimed. */
export const toggleCallout = StateEffect.define({
  map: (pos, changes) => changes.mapPos(pos),
});

/* The callout's header: caret + type when open (the title text stays
   real, editable text on the line); caret + type + title + a line count
   when the whole block is folded behind it. */
class CalloutHead extends WidgetType {
  constructor(type, title, count, at, folded) {
    super();
    this.type = type;
    this.title = title;
    this.count = count;
    this.at = at;           // the title line's start — the fold's key
    this.folded = folded;
  }
  eq(o) {
    return o.type === this.type && o.title === this.title
      && o.count === this.count && o.at === this.at && o.folded === this.folded;
  }
  toDOM(view) {
    const el = document.createElement(this.folded ? "div" : "span");
    el.className = "cm-callout-head" + (this.folded ? " folded" : "");
    const caret = document.createElement("span");
    caret.className = "cm-callout-caret";
    caret.textContent = this.folded ? "▸" : "▾";
    el.appendChild(caret);
    const label = document.createElement("span");
    label.className = "cm-callout-type";
    label.textContent = this.type.toUpperCase();
    el.appendChild(label);
    if (this.folded) {
      if (this.title) el.appendChild(document.createTextNode(" " + this.title));
      const n = document.createElement("span");
      n.className = "cm-callout-count";
      n.textContent = ` · ${this.count} line${this.count === 1 ? "" : "s"}`;
      el.appendChild(n);
    }
    el.onmousedown = (e) => {
      e.preventDefault();
      view.dispatch({ effects: toggleCallout.of(this.at) });
    };
    return el;
  }
  ignoreEvent() {
    return false;   // the click is ours, not the editor's
  }
}

/* Every callout's decorations, from (doc, selection, toggles) alone —
   the field recomputes only when one of those moved. `blocks` rides
   along so the view plugin can skip the claimed lines. A caret anywhere
   inside a block forces the raw source — nothing is hidden that the
   caret can't reach. */
function calloutCompute(doc, selection, toggles) {
  const marks = [];
  const blocks = [];
  const active = new Set();
  for (const r of selection.ranges) {
    const from = doc.lineAt(r.from).number;
    const to = doc.lineAt(r.to).number;
    for (let n = from; n <= to; n++) active.add(n);
  }
  for (let n = 1; n <= doc.lines; n++) {
    const line = doc.line(n);
    const m = CALLOUT_RE.exec(line.text);
    if (!m) continue;
    let end = line;
    while (end.number < doc.lines) {
      const nxt = doc.line(end.number + 1);
      if (!QUOTE_PREFIX_RE.test(nxt.text)) break;
      end = nxt;
    }
    let blockLive = false;
    for (let k = line.number; k <= end.number && !blockLive; k++)
      if (active.has(k)) blockLive = true;
    const folded = (m[2] === "-") !== toggles.has(line.from);
    const bodyLines = end.number - line.number;
    if (folded && !blockLive && bodyLines > 0) {
      blocks.push({ from: line.from, to: end.to });   // plugin skips it all
      marks.push(Decoration.replace({
        widget: new CalloutHead(m[1], m[3], bodyLines, line.from, true),
        block: true,
      }).range(line.from, end.to));
    } else {
      // Open: only the TITLE line is claimed — the body lines flow
      // through the plugin's normal inline rules, so a [[source]] cited
      // inside a callout renders (and clicks) like one anywhere else.
      blocks.push({ from: line.from, to: line.to });
      marks.push(Decoration.line({ class: "cm-callout-title" }).range(line.from));
      if (!active.has(line.number))
        marks.push(Decoration.replace({
          widget: new CalloutHead(m[1], "", bodyLines, line.from, false),
        }).range(line.from, line.from + line.text.length - m[3].length));
      for (let k = line.number + 1; k <= end.number; k++) {
        const b = doc.line(k);
        marks.push(Decoration.line({ class: "cm-callout" }).range(b.from));
        const q = QUOTE_PREFIX_RE.exec(b.text);
        if (!active.has(k) && q && q[0].length)
          marks.push(Decoration.replace({}).range(b.from, b.from + q[0].length));
      }
    }
    n = end.number;   // the body is claimed; resume past it
  }
  return { deco: RangeSet.of(marks, true), blocks };
}

export const calloutField = StateField.define({
  create: (state) => ({ toggles: new Set(),
                        ...calloutCompute(state.doc, state.selection, new Set()) }),
  update(value, tr) {
    let toggles = value.toggles;
    if (tr.docChanged) {
      toggles = new Set();
      for (const p of value.toggles) toggles.add(tr.changes.mapPos(p));
    }
    for (const e of tr.effects) {
      if (e.is(toggleCallout)) {
        if (toggles === value.toggles) toggles = new Set(toggles);
        toggles.has(e.value) ? toggles.delete(e.value) : toggles.add(e.value);
      }
    }
    // tr.newDoc/newSelection, never tr.state: a field reading the new
    // state mid-computation is a re-entrancy error waiting to happen
    if (!tr.docChanged && !tr.selection && toggles === value.toggles) return value;
    return { toggles, ...calloutCompute(tr.newDoc, tr.newSelection, toggles) };
  },
  provide: (f) => EditorView.decorations.from(f, (v) => v.deco),
});

/* A list marker reads as a bullet, not as the hyphen you typed. Same
   deal as the checkbox: the file still says "- ". */
class Bullet extends WidgetType {
  eq() {
    return true;
  }
  toDOM() {
    const dot = document.createElement("span");
    dot.className = "cm-bullet";
    dot.textContent = "•";
    return dot;
  }
}

/* Wikilinks NAVIGATE — the accent and pointer cursor were a promise
   this extension keeps. Click resolution goes through the document
   position, not the span's text: syntax highlighting can split a link
   across spans, but the position always lands inside exactly one
   [[…]] match on its line. `onOpen(path, target)` gets the link with
   alias stripped and any #fragment as the reveal target. */
export const wikilinkClick = (onOpen) => EditorView.domEventHandlers({
  click: (e, view) => {
    if (!e.target.closest?.(".cm-wikilink")) return false;
    const pos = view.posAtDOM(e.target, 0);
    const line = view.state.doc.lineAt(pos);
    WIKILINK_RE.lastIndex = 0;
    for (let m; (m = WIKILINK_RE.exec(line.text)) !== null; ) {
      const from = line.from + m.index;
      if (pos >= from && pos <= from + m[0].length) {
        const [path, frag = ""] = m[1].split("|")[0].split("#");
        if (path.trim()) onOpen(path.trim(), frag.trim());
        return true;
      }
    }
    return false;
  },
});

/* Lines the caret touches: those render as raw markdown so the syntax
   is editable. Everything else gets the rendered treatment. */
function activeLines(state) {
  const lines = new Set();
  for (const r of state.selection.ranges) {
    const from = state.doc.lineAt(r.from).number;
    const to = state.doc.lineAt(r.to).number;
    for (let n = from; n <= to; n++) lines.add(n);
  }
  return lines;
}

function frontmatterEnd(doc) {
  if (doc.lines < 2 || doc.line(1).text !== "---") return 0;
  for (let n = 2; n <= doc.lines; n++) if (doc.line(n).text === "---") return n;
  return 0;
}

function build(view) {
  const { state } = view;
  const active = activeLines(state);
  const fmEnd = frontmatterEnd(state.doc);
  const dates = state.field(planDatesField, false) || { today: "", week: "" };
  const callouts = state.field(calloutField, false)?.blocks ?? [];
  const marks = [];
  const add = (from, to, deco) => marks.push(deco.range(from, to));
  const hide = (from, to) => add(from, to, Decoration.replace({}));

  for (const { from: vFrom, to: vTo } of view.visibleRanges) {
    let pos = vFrom;
    while (pos <= vTo) {
      const line = state.doc.lineAt(pos);
      const text = line.text;
      const live = active.has(line.number);   // caret here: show the source

      /* frontmatter — config, not prose: dimmed and set in mono, never
         hidden (it holds the entity id and the vault spec) */
      if (fmEnd && line.number <= fmEnd) {
        add(line.from, line.from, Decoration.line({ class: "cm-frontmatter" }));
        pos = line.to + 1;
        continue;
      }

      /* callout blocks belong to calloutField (block decorations must
         come from a state field) — skip every line it has claimed */
      if (callouts.some((b) => line.from >= b.from && line.from <= b.to)) {
        pos = line.to + 1;
        continue;
      }

      const heading = HEADING_RE.exec(text);
      if (heading) {
        add(line.from, line.from,
            Decoration.line({ class: `cm-h cm-h${heading[1].length}` }));
        if (!live) hide(line.from, line.from + heading[0].length);
      }

      const task = TASK_RE.exec(text);
      if (task) {
        const checked = task[3].toLowerCase() === "x";
        add(line.from, line.from,
            Decoration.line({ class: "cm-task" + (checked ? " done" : "") }));
        if (!live) {
          const indent = task[1].length;
          const markPos = line.from + indent + 3;   // "- [" is 3 chars
          add(line.from + indent, line.from + task[0].length,
              Decoration.replace({ widget: new TaskBox(checked, markPos) }));
        }
        /* Strike the TEXT, not the line. Striking the line drags the rule
           through the trailing metadata and the gaps between chips, which
           renders as stray dashes floating between them. */
        if (checked) {
          const bodyStart = line.from + task[0].length;
          let bodyEnd = line.to;
          for (const re of [BLOCK_REF_RE, DATE_RE, PRIORITY_RE]) {
            re.lastIndex = 0;
            const m = re.exec(text);
            if (m) bodyEnd = Math.min(bodyEnd, line.from + m.index);
          }
          const trimmed = bodyEnd - (text.slice(0, bodyEnd - line.from).length
                                     - text.slice(0, bodyEnd - line.from).trimEnd().length);
          if (trimmed > bodyStart)
            add(bodyStart, trimmed, Decoration.mark({ class: "cm-struck" }));
        }
        /* Open tasks get the plan chips. side: 1 keeps them past the
           hidden block ref, which replaces the run up TO line.to. */
        if (!checked && dates.today)
          add(line.to, line.to, Decoration.widget({
            widget: new PlanChips(line.from, (SCHEDULED_RE.exec(text) || [])[1] || "", dates),
            side: 1,
          }));
      }

      const bullet = !task && BULLET_RE.exec(text);
      if (bullet && !live) {
        const at = line.from + bullet[1].length;
        add(at, at + 1, Decoration.replace({ widget: new Bullet() }));
      }

      /* block refs are pure machinery — the watermark the engine stamps.
         Hidden off-line, because a note full of ^t-7d2a408e0209 is
         unreadable, and revealed the moment you might need to edit it. */
      const ref = BLOCK_REF_RE.exec(text);
      if (ref) {
        const at = line.from + ref.index;
        if (live) add(at, line.to, Decoration.mark({ class: "cm-ref" }));
        else hide(at, line.to);
      }

      const scan = (re, fn) => {
        re.lastIndex = 0;
        let m;
        while ((m = re.exec(text)) !== null) fn(m, line.from + m.index);
      };

      /* Dates: the chip carries the meaning, so the emoji is redundant
         weight — full-colour glyphs shout in a palette this quiet. It's
         hidden off-line and the date is styled instead. ➕ (created) goes
         entirely: it's provenance, not something you read a note for. */
      scan(DATE_RE, (m, at) => {
        const kind = m[1] === "✅" ? "done"
          : m[1] === "➕" ? "created"
          : (m[1] === "⏳" || m[1] === "⌛") ? "planned" : "due";
        if (!live && kind === "created") {
          hide(at, at + m[0].length);
          return;
        }
        if (!live) hide(at, at + m[1].length + (m[0][m[1].length] === " " ? 1 : 0));
        add(at, at + m[0].length, Decoration.mark({ class: `cm-date ${kind}` }));
      });
      scan(PRIORITY_RE, (m, at) =>
        add(at, at + m[0].length, Decoration.mark({ class: "cm-prio" })));

      scan(WIKILINK_RE, (m, at) => {
        add(at, at + m[0].length, Decoration.mark({ class: "cm-wikilink" }));
        if (!live) {
          hide(at, at + 2);
          hide(at + m[0].length - 2, at + m[0].length);
        }
      });

      scan(BOLD_RE, (m, at) => {
        add(at, at + m[0].length, Decoration.mark({ class: "cm-strong" }));
        if (!live) {
          hide(at, at + 2);
          hide(at + m[0].length - 2, at + m[0].length);
        }
      });

      pos = line.to + 1;
    }
  }
  // sort=true: line decorations and marks are collected out of order above
  return RangeSet.of(marks, true);
}

export const livePreview = ViewPlugin.fromClass(
  class {
    constructor(view) {
      this.decorations = build(view);
    }
    update(u) {
      // selection matters as much as content: moving the caret is what
      // reveals and re-hides syntax — and a fold toggle redraws too
      // (the claimed-lines skip must track the field's blocks)
      if (u.docChanged || u.selectionSet || u.viewportChanged
          || u.startState.field(calloutField, false)
             !== u.state.field(calloutField, false))
        this.decorations = build(u.view);
    }
  },
  { decorations: (v) => v.decorations },
);

/* Theme in the app's own tokens, so the editor is part of the product
   rather than an embedded widget with its own opinions. */
export const vaultTheme = EditorView.theme({
  "&": { color: "var(--ink)", backgroundColor: "transparent",
         fontSize: "var(--text-body)" },
  ".cm-content": {
    fontFamily: "var(--font)", lineHeight: "var(--leading)",
    padding: "0 0 40vh", caretColor: "var(--accent)",
  },
  "&.cm-focused": { outline: "none" },
  ".cm-line": { padding: "0 2px" },
  ".cm-gutters": { display: "none" },
  ".cm-activeLine": { backgroundColor: "transparent" },
  ".cm-selectionBackground, ::selection": { backgroundColor: "var(--accent-soft)" },
  "&.cm-focused .cm-selectionBackground": { backgroundColor: "var(--accent-soft)" },

  ".cm-frontmatter": {
    fontFamily: "var(--font-mono)", fontSize: "12px",
    color: "var(--ink-3)", backgroundColor: "var(--hover)",
  },
  ".cm-h": { fontWeight: "600", lineHeight: "1.35" },
  ".cm-h1": { fontSize: "1.5em" },
  ".cm-h2": { fontSize: "1.22em" },
  ".cm-h3": { fontSize: "1.06em" },
  ".cm-h4, .cm-h5, .cm-h6": { fontSize: "1em", color: "var(--ink-2)" },

  ".cm-task.done": { color: "var(--ink-3)" },
  ".cm-struck": { textDecoration: "line-through" },
  ".cm-taskbox": {
    display: "inline-block", width: "13px", height: "13px",
    marginRight: "7px", verticalAlign: "-1px",
    border: "1.5px solid var(--ink-3)", borderRadius: "3px",
    cursor: "pointer", boxSizing: "border-box",
  },
  ".cm-taskbox.on": {
    backgroundColor: "var(--ok)", borderColor: "var(--ok)",
    backgroundImage:
      "url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'><path d='M2.5 6.2l2.2 2.2 4.6-4.6' fill='none' stroke='white' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/></svg>\")",
    backgroundSize: "11px 11px", backgroundPosition: "center",
    backgroundRepeat: "no-repeat",
  },

  ".cm-target": {
    backgroundColor: "var(--accent-soft)",
    boxShadow: "inset 2px 0 0 var(--accent)",
    borderRadius: "2px",
    transition: "background-color 400ms ease-out, box-shadow 400ms ease-out",
  },
  ".cm-bullet": {
    display: "inline-block", width: "1em",
    color: "var(--ink-3)", textAlign: "left",
  },

  ".cm-ref": { color: "var(--ink-3)", fontFamily: "var(--font-mono)", fontSize: "11.5px" },
  ".cm-date": {
    // inline-block on purpose: text-decoration propagates from the struck
    // task line and a descendant can't opt out of it any other way — the
    // line through a completed task must stop at its text.
    display: "inline-block", textDecoration: "none",
    fontSize: "var(--text-small)", color: "var(--ink-2)",
    backgroundColor: "var(--hover)", borderRadius: "10px",
    padding: "0 7px", marginLeft: "2px", verticalAlign: "1px",
  },
  ".cm-date.done": { color: "var(--ok)", backgroundColor: "var(--diff-add-row)" },
  ".cm-date.created": { color: "var(--ink-3)", backgroundColor: "transparent", padding: "0" },
  // a plan is not a deadline: ⏳ reads quieter than 📅 on purpose
  ".cm-date.planned": { color: "var(--accent)", backgroundColor: "var(--accent-soft)" },

  /* Same bargain as the dashboard's .affordance: present on the row you
     are pointing at, invisible otherwise. A note is for reading, and two
     chips on every open task would make it a control panel. */
  ".cm-plan": {
    opacity: "0", transition: "opacity 120ms ease",
    marginLeft: "8px", whiteSpace: "nowrap", verticalAlign: "1px",
  },
  ".cm-task:hover .cm-plan, .cm-target .cm-plan, .cm-plan:focus-within": { opacity: "1" },
  ".cm-plan-btn": {
    font: "inherit", fontSize: "var(--text-small)", color: "var(--ink-3)",
    background: "none", border: "0", padding: "0 4px", cursor: "pointer",
  },
  ".cm-plan-btn:hover": { color: "var(--accent)", textDecoration: "underline" },
  ".cm-plan-btn.on": { color: "var(--accent)" },
  ".cm-prio": { color: "var(--accent)" },
  ".cm-wikilink": { color: "var(--accent)", cursor: "pointer" },
  ".cm-strong": { fontWeight: "600" },

  /* Callouts: one quiet, clickable header; the body reads as a quote.
     Folded, the header row is all there is — a pasted transcript is a
     single line until asked for. */
  ".cm-callout-head": { cursor: "pointer", color: "var(--ink-2)" },
  ".cm-callout-head.folded": {
    padding: "2px 8px", margin: "2px 0",
    backgroundColor: "var(--hover)", borderLeft: "2px solid var(--line)",
    borderRadius: "4px",
  },
  ".cm-callout-caret": { display: "inline-block", width: "1em", color: "var(--ink-3)" },
  ".cm-callout-type": {
    fontWeight: "600", fontSize: "var(--text-small)",
    letterSpacing: "0.04em", marginRight: "6px",
  },
  ".cm-callout-count": { color: "var(--ink-3)", fontSize: "var(--text-small)" },
  ".cm-callout-title": {
    backgroundColor: "var(--hover)", borderLeft: "2px solid var(--line)",
    borderRadius: "4px 4px 0 0",
  },
  ".cm-callout": {
    borderLeft: "2px solid var(--line)", color: "var(--ink-2)",
    backgroundColor: "var(--hover)",
  },
});
