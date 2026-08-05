import { RangeSet, StateEffect, StateField } from "@codemirror/state";
import { Decoration, EditorView, ViewPlugin, WidgetType } from "@codemirror/view";
import { postJSON } from "./api.js";
import { planTargets } from "./plan.js";
import { WIKILINK_RE, parseWikilink } from "./wiki.js";

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

   The patterns mirror chickenskratch_vault/tasks.py and ids.py exactly — this
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
// the chips' placement inputs: the line's own 📅 and 👤, same glyphs
// tasks.py reads (DATE_RE above is the rendering scan; this one feeds
// planTargets, which needs the due date alone)
const DUE_RE = /📅\s*(\d{4}-\d{2}-\d{2})/;
const BOLD_RE = /\*\*([^*]+)\*\*/g;
const HEADING_RE = /^(#{1,6})\s+/;
// A callout title: `> [!NOTE]- Verbatim — 14:32` (the space after > is
// optional — hand-typed `>[!NOTE]` is the same callout). `-` marks it
// collapsed by default; the body is every following quoted line. This
// is the block the record pipeline folds a dump's verbatim into, so a
// pasted transcript must read as one collapsed row, not a wall.
const CALLOUT_RE = /^>\s*\[!(\w+)\]([-+]?)\s?(.*)$/;
const QUOTE_PREFIX_RE = /^\s*>\s?/;

/* One splice for every line affordance (the checkbox, the plan chips):
   ask the server for the rewritten line, then apply it as a DOCUMENT
   edit. The round trip is localhost-fast but not instant, and the
   document is live under it — so the rewrite lands only if the line
   still says what we sent; a keystroke that landed meanwhile owns the
   line, and we must not paste a stale rewrite over it. The "affordance"
   userEvent makes the editor save a clean buffer immediately: the click
   is the dashboard's verb, not a keystroke, and a silent buffer edit
   still waiting on ⌘S reads as "it didn't happen" from the Today
   column. */
async function rewriteLine(view, lineFrom, req, userEvent) {
  const before = view.state.doc.lineAt(lineFrom).text;
  let next;
  try {
    ({ line: next } = await postJSON("/api/task/line", { line: before, ...req }));
  } catch {
    return;                    // server unreachable: the line stands
  }
  const now = view.state.doc.lineAt(lineFrom);
  if (now.text === before && next !== before)
    view.dispatch({ changes: { from: now.from, to: now.to, insert: next },
                    userEvent });
}

/* The checkbox: a real affordance over plain text — we drew a box on
   top of an unchanged `- [ ]`. The rewrite comes from the server
   (/api/task/line {line, done}): tasks.set_line_status, the SAME
   completion rewrite behind the dashboard's done verb, so completing
   here stamps ✅ and reopening strips it exactly as it does everywhere
   else. (Its first life as a raw one-character mark flip left
   editor-completed tasks with no done date and reopened ones with a
   stale ✅ — two surfaces writing different grammar.) */
class TaskBox extends WidgetType {
  constructor(checked, lineFrom) {
    super();
    this.checked = checked;
    this.lineFrom = lineFrom;
  }
  eq(other) {
    return other.checked === this.checked && other.lineFrom === this.lineFrom;
  }
  toDOM(view) {
    const box = document.createElement("span");
    box.className = "cm-taskbox" + (this.checked ? " on" : "");
    box.setAttribute("role", "checkbox");
    box.setAttribute("aria-checked", String(this.checked));
    box.title = this.checked ? "Reopen" : "Complete";
    box.onmousedown = (e) => {
      e.preventDefault();
      rewriteLine(view, this.lineFrom, { done: !this.checked }, "affordance.task");
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
  create: () => ({ today: "", week: "", next: "" }),
  update(dates, tr) {
    for (const e of tr.effects) if (e.is(setPlanDates)) return e.value;
    return dates;
  },
});

/* Plan chips on an open task: the same ⏳ verb the dashboard's rows
   offer, on the surface where you're actually reading the task. Which
   chips render comes from planTargets (plan.js) — the ONE placement
   rule both surfaces share — fed with this line's own ⏳/📅/👤, so a
   task shows the two places it is NOT, never a move its dates make a
   lie, exactly as its dashboard row does.

   The rewrite itself comes from the server (/api/task/line), which
   returns the new line and writes nothing. Two shapes were rejected:
   /api/task/plan would change the file under a buffer that may hold
   unsaved edits, forcing the stale-hash reload that discards them; and
   a JS reimplementation would put this vault's task grammar in two
   languages, with the engine re-reading whatever we wrote using the
   Python regexes. Like the checkbox, what lands here is a DOCUMENT
   edit — ⌘S is still the write, through the same gated /api/note. */
class PlanChips extends WidgetType {
  constructor(lineFrom, planned, due, owed, dates) {
    super();
    this.lineFrom = lineFrom;
    this.planned = planned;      // the ⏳ date already on the line, or ""
    this.due = due;              // the 📅 date on the line, or ""
    this.owed = owed;            // 👤 on the line: the plan is a snooze
    this.dates = dates;
  }
  eq(other) {
    return other.lineFrom === this.lineFrom && other.planned === this.planned
      && other.due === this.due && other.owed === this.owed
      && other.dates.today === this.dates.today
      && other.dates.week === this.dates.week
      && other.dates.next === this.dates.next;
  }
  toDOM(view) {
    const wrap = document.createElement("span");
    wrap.className = "cm-plan";
    const targets = planTargets(
      { due: this.due, scheduled: this.planned, owed: this.owed }, this.dates);
    for (const t of targets) {
      const b = document.createElement("button");
      b.className = "cm-plan-btn";
      b.textContent = t.text;
      b.title = t.when ? `Plan for ${t.when} (⏳)`
        : "Clear the plan (⏳) — back to the long-term queue";
      b.onmousedown = (e) => {
        e.preventDefault();          // never steal the caret from the line
        rewriteLine(view, this.lineFrom, { when: t.when }, "affordance.plan");
      };
      wrap.appendChild(b);
    }
    return wrap;
  }
  ignoreEvent() {
    return false;   // the click is ours, not the editor's
  }
}

/* Callout folding. The default comes from the marker (`-` starts
   collapsed); clicking the header toggles, and the toggle is remembered
   per block — positions map through edits, so typing above a folded
   verbatim doesn't spring it open. Open callouts are additionally
   LOCKED by default (see toggleCalloutLock): reading surfaces first,
   markdown on request. Like every rule here it is a view decision: the
   bytes on disk never change.

   This layer lives in a StateField, not the view plugin, because a
   folded block is a `block: true` replace decoration — those change
   vertical layout, and CodeMirror refuses them from plugins ("block
   decorations may not be specified via plugins"). One field owns the
   toggles AND the decorations built from them; the plugin just skips
   the lines this field has claimed. */
export const toggleCallout = StateEffect.define({
  map: (pos, changes) => changes.mapPos(pos),
});

/* The lock: callouts are READERS by default. A click inside a locked
   callout never places the caret — so a misclick can't flip the block
   into raw markdown under you. The head's edit button unlocks; keyboard
   caret movement into the block also reveals the raw source, on purpose:
   arrowing in is deliberate in a way a stray click is not, and hiding
   text the caret can reach would let edits land on bytes you can't see.
   Unlocking is per block and per session (default relocks on reopen),
   same position-keyed mechanism as the fold toggles. */
export const toggleCalloutLock = StateEffect.define({
  map: (pos, changes) => changes.mapPos(pos),
});

/* The callout's header: caret + type when open (the title text stays
   real, editable text on the line); caret + type + title + a line count
   when the whole block is folded behind it. */
class CalloutHead extends WidgetType {
  constructor(type, title, count, at, folded, locked) {
    super();
    this.type = type;
    this.title = title;
    this.count = count;
    this.at = at;           // the title line's start — the fold's key
    this.folded = folded;
    this.locked = locked;
  }
  eq(o) {
    return o.type === this.type && o.title === this.title
      && o.count === this.count && o.at === this.at && o.folded === this.folded
      && o.locked === this.locked;
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
    if (!this.folded) {
      // The one way into a locked callout's markdown. Its own mousedown,
      // stopped, so the head's fold toggle doesn't fire on the same click.
      const lock = document.createElement("button");
      lock.className = "cm-callout-lock" + (this.locked ? "" : " on");
      lock.textContent = this.locked ? "edit" : "lock";
      lock.title = this.locked
        ? "Unlock — clicks place the caret and reveal the raw markdown"
        : "Lock — clicks read; the markdown stays put";
      lock.onmousedown = (e) => {
        e.preventDefault();
        e.stopPropagation();
        view.dispatch({ effects: toggleCalloutLock.of(this.at) });
      };
      el.appendChild(lock);
    }
    return el;
  }
  ignoreEvent() {
    return false;   // the click is ours, not the editor's
  }
}

/* Every callout's decorations, from (doc, selection, toggles, unlocks)
   alone — the field recomputes only when one of those moved. `blocks`
   rides along so the view plugin can skip the claimed lines; `guarded`
   is the locked blocks' ranges for the mousedown guard below. A caret
   inside ANY block — locked or not — forces the raw source: nothing is
   ever hidden that the caret can reach, or keyboard edits would land on
   invisible bytes. The lock's whole job is done earlier, by the guard
   that keeps a CLICK from placing the caret there. */
function calloutCompute(doc, selection, toggles, unlocks) {
  const marks = [];
  const blocks = [];
  const guarded = [];
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
    const locked = !unlocks.has(line.from);
    let blockLive = false;
    for (let k = line.number; k <= end.number && !blockLive; k++)
      if (active.has(k)) blockLive = true;
    const folded = (m[2] === "-") !== toggles.has(line.from);
    const bodyLines = end.number - line.number;
    if (folded && !blockLive && bodyLines > 0) {
      blocks.push({ from: line.from, to: end.to });   // plugin skips it all
      marks.push(Decoration.replace({
        widget: new CalloutHead(m[1], m[3], bodyLines, line.from, true, locked),
        block: true,
      }).range(line.from, end.to));
    } else {
      // Open: only the TITLE line is claimed — the body lines flow
      // through the plugin's normal inline rules, so a [[source]] cited
      // inside a callout renders (and clicks) like one anywhere else.
      blocks.push({ from: line.from, to: line.to });
      // No guard while the caret is inside: the block is already showing
      // raw source, and a click then is repositioning, not a misclick.
      if (locked && !blockLive) guarded.push({ from: line.from, to: end.to });
      marks.push(Decoration.line({ class: "cm-callout-title" }).range(line.from));
      if (!active.has(line.number))
        marks.push(Decoration.replace({
          widget: new CalloutHead(m[1], "", bodyLines, line.from, false, locked),
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
  return { deco: RangeSet.of(marks, true), blocks, guarded };
}

const mapSet = (set, changes) => {
  const out = new Set();
  for (const p of set) out.add(changes.mapPos(p));
  return out;
};

export const calloutField = StateField.define({
  create: (state) => ({ toggles: new Set(), unlocks: new Set(),
                        ...calloutCompute(state.doc, state.selection,
                                          new Set(), new Set()) }),
  update(value, tr) {
    let { toggles, unlocks } = value;
    if (tr.docChanged) {
      toggles = mapSet(toggles, tr.changes);
      unlocks = mapSet(unlocks, tr.changes);
    }
    const flip = (set, orig, pos) => {
      if (set === orig) set = new Set(set);
      set.has(pos) ? set.delete(pos) : set.add(pos);
      return set;
    };
    for (const e of tr.effects) {
      if (e.is(toggleCallout)) toggles = flip(toggles, value.toggles, e.value);
      if (e.is(toggleCalloutLock)) unlocks = flip(unlocks, value.unlocks, e.value);
    }
    // tr.newDoc/newSelection, never tr.state: a field reading the new
    // state mid-computation is a re-entrancy error waiting to happen
    if (!tr.docChanged && !tr.selection
        && toggles === value.toggles && unlocks === value.unlocks) return value;
    return { toggles, unlocks,
             ...calloutCompute(tr.newDoc, tr.newSelection, toggles, unlocks) };
  },
  provide: (f) => [
    EditorView.decorations.from(f, (v) => v.deco),
    /* The lock's teeth: mousedown inside a locked block is consumed
       before it can place the caret, so the block never flips to raw
       markdown on a misclick. Real affordances inside the block —
       wikilinks, the head, the lock button itself — handle their own
       events and are let through (a wikilink navigates on click, which
       still fires; only the caret placement dies). Mouse text-selection
       inside a locked callout dies with it: unlock to select. */
    EditorView.domEventHandlers({
      mousedown: (e, view) => {
        if (e.target.closest?.(".cm-callout-head, .cm-callout-lock, .cm-wikilink"))
          return false;
        const pos = view.posAtCoords({ x: e.clientX, y: e.clientY });
        if (pos == null) return false;
        if (view.state.field(f).guarded.some((b) => pos >= b.from && pos <= b.to)) {
          e.preventDefault();
          return true;
        }
        return false;
      },
    }),
  ],
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
        // split by the shared grammar (wiki.js) — same as text.jsx
        const { path, frag } = parseWikilink(m[1]);
        if (path) onOpen(path, frag);
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
  const dates = state.field(planDatesField, false)
    || { today: "", week: "", next: "" };
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
          add(line.from + indent, line.from + task[0].length,
              Decoration.replace({ widget: new TaskBox(checked, line.from) }));
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
            widget: new PlanChips(line.from,
                                  (SCHEDULED_RE.exec(text) || [])[1] || "",
                                  (DUE_RE.exec(text) || [])[1] || "",
                                  text.includes("👤"), dates),
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
          // [[target|alias]] shows only the alias: the target — and any
          // #quote fragment riding on it as a jump destination (//ask's
          // source links) — is machinery, hidden with the brackets.
          const pipe = m[1].indexOf("|");
          hide(at, at + 2 + (pipe >= 0 ? pipe + 1 : 0));
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
  /* Same bargain as the plan chips: present when pointed at, invisible
     otherwise — except while UNLOCKED, when it stays lit so an editable
     callout is never a surprise. */
  ".cm-callout-lock": {
    font: "inherit", fontSize: "var(--text-small)", color: "var(--ink-3)",
    background: "none", border: "0", padding: "0 4px", marginLeft: "6px",
    cursor: "pointer", opacity: "0", transition: "opacity 120ms ease",
  },
  ".cm-callout-title:hover .cm-callout-lock, .cm-callout-lock:focus": { opacity: "1" },
  ".cm-callout-lock:hover": { color: "var(--accent)", textDecoration: "underline" },
  ".cm-callout-lock.on": { color: "var(--accent)", opacity: "1" },
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
