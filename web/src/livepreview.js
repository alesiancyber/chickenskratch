import { RangeSet, StateEffect, StateField } from "@codemirror/state";
import { Decoration, EditorView, ViewPlugin, WidgetType } from "@codemirror/view";
import { BLOCK_REF_RE, SCHEDULED_RE, setLineScheduled } from "./taskline.js";

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
const PRIORITY_RE = /[🔺⏫🔼🔽⏬]/g;
const WIKILINK_RE = /\[\[([^\]]+)\]\]/g;
const BOLD_RE = /\*\*([^*]+)\*\*/g;
const HEADING_RE = /^(#{1,6})\s+/;

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
      view.dispatch({
        changes: { from: this.markPos, to: this.markPos + 1,
                   insert: this.checked ? " " : "x" },
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
   offer, on the surface where you're actually reading the task. Like
   the checkbox, this edits the DOCUMENT — it does not call
   /api/task/plan. That is deliberate: the API writes the file on the
   server, which would stale the buffer you have open and force a
   reload that discards unsaved edits. The text is the file; ⌘S is
   still the write, and it goes through the same gated /api/note path
   as any other edit here. */
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
      b.onmousedown = (e) => {
        e.preventDefault();          // never steal the caret from the line
        const line = view.state.doc.lineAt(this.lineFrom);
        const next = setLineScheduled(line.text, on ? null : when);
        if (next !== line.text)
          view.dispatch({ changes: { from: line.from, to: line.to, insert: next } });
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
      // reveals and re-hides syntax
      if (u.docChanged || u.selectionSet || u.viewportChanged)
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
});
