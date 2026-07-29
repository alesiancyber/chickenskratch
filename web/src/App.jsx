import * as Collapsible from "@radix-ui/react-collapsible";
import { Command } from "cmdk";
import {
  createContext, lazy, Suspense, useCallback, useContext, useEffect,
  useMemo, useRef, useState,
} from "react";
import { cancelJob, noteName, postJSON, useDashboard, useSplit } from "./api.js";
import Canvas from "./Canvas.jsx";
import Diff from "./Diff.jsx";
import Explorer from "./Explorer.jsx";
import Console from "./Console.jsx";
import Pane from "./Pane.jsx";

/* CodeMirror triples the bundle, and the dashboard is what you see
   first — so the editor loads on the first note you open, not on
   startup. Localhost makes the fetch instant. */
const NoteEditor = lazy(() => import("./NoteEditor.jsx"));

/* Opening a note is available from every row in the tree, so it rides a
   context rather than being threaded through five components. */
const OpenNote = createContext(() => {});

/* Click-select: the row you clicked stays lit while the editor marks its
   line — both sides of the same click. Focus mode reads this selection:
   with it on, every unselected task row dims past readability. A context
   for the same reason as OpenNote: every NoteRow participates. */
const SelectRow = createContext({ key: null, set: () => {} });

/* The owned attention/decision surface. Four regions, one question
   each, one verb each — do (today), start (lands soon), nudge (stalled
   on someone else), decide (captured, not yet dispositioned) — and
   everything that answers no question (in flight, history) folds under
   Reference. The empty state is the success condition. Optimistic +
   undoable, never modal — actions apply instantly, undo IS revert(sha)
   (the git-snapshot guarantee surfaced as UX). Keyboard-first: j/k walk
   decisions, a/r decide, e opens the editor, u undoes, ⌘J summons the
   escape hatch, ⌘K everything else.

   Layout: decisions/tasks left, canvas right, one draggable divider
   between them — both surfaces live at once, because dumping and
   deciding are the same sitting. The file explorer is a rail outside
   that split (⌘B, collapsed by default) so browsing the vault by hand
   never re-proportions the two surfaces you actually work in. The
   escape hatch docks bottom-right, minimizable, so its tool activity is
   visible without costing a column. */

const FOLD = 8;

/* The small model, working. Deliberately a whisper — a pulse and a
   phrase in the nav, not a modal or a progress bar we'd have to lie
   about. It doubles as the stop button's home: the only moment you
   want to stop something is while it's telling you it's running. */
function Activity({ activity, onStop }) {
  if (!activity.running) return null;
  return (
    <span className="activity" role="status">
      <span className="pulse" />
      <span className="ghost">{activity.stopping ? "stopping…" : activity.label}</span>
      {!activity.stopping && (
        <button className="stop-btn" onClick={onStop} title="Stop this pass">
          stop
        </button>
      )}
    </span>
  );
}

/* Drag to re-proportion the two columns. Pointer events (not mouse) so
   trackpad and touch behave; listeners live on window so a fast drag
   that outruns the 6px handle doesn't drop the gesture. */
function Splitter({ onDrag }) {
  const onPointerDown = (e) => {
    e.preventDefault();
    document.body.classList.add("resizing");
    const move = (ev) => onDrag(ev.clientX);
    const up = () => {
      document.body.classList.remove("resizing");
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  return (
    <div className="splitter" onPointerDown={onPointerDown}
         role="separator" aria-orientation="vertical"
         aria-label="Resize columns" />
  );
}

/* Any row that stands for a note: clicking anywhere on it opens that note
   in the one live editor. One page, one open note — nothing to manage.
   `target` is the row's own text, so the editor lands on that line and
   marks it instead of dropping you at the top to go hunting. */
function NoteRow({ note, target = "", className = "", children }) {
  const open = useContext(OpenNote);
  const { key: selKey, set: setSel } = useContext(SelectRow);
  const myKey = note + "\u0000" + target;
  const go = () => {
    open(note, target);
    setSel(myKey === selKey ? null : myKey);   // second click releases it
  };
  return (
    <div className={"row noterow " + className + (selKey === myKey ? " sel" : "")}
         role="button" tabIndex={0}
         onClick={go}
         onKeyDown={(e) => {
           if (e.key === "Enter" || e.key === " ") {
             e.preventDefault();
             go();
           }
         }}>
      {children}
    </div>
  );
}

function Rows({ items, render, fold = FOLD, label = "" }) {
  const [all, setAll] = useState(false);
  const shown = all ? items : items.slice(0, fold);
  return (
    <>
      {shown.map(render)}
      {items.length > fold && !all && (
        <div className="fold small">
          <button className="linklike" onClick={() => setAll(true)}>
            +{items.length - fold} more {label}
          </button>
        </div>
      )}
    </>
  );
}

/* One question per region, and the heading IS the question — if a region
   can't be phrased as something you'd ask, it's decoration and belongs
   under Reference. Each region carries exactly one verb (do / start /
   nudge / decide), so nothing renders twice under two names. */
function Section({ id, label, verb, children }) {
  return (
    <section id={id}>
      <div className="section-label">
        {label}
        {verb && <span className="section-verb">{verb}</span>}
      </div>
      {children}
    </section>
  );
}

/* One key shape for the optimistic done-strike set. NUL as separator
   because note paths and task text can both contain anything printable;
   String.fromCharCode(0) equals the "\u0000" literal used where the set
   is built. */
const doneKey = (r) => r.note + String.fromCharCode(0) + r.text;

/* a task row with the prototype's checkbox: click = done, through the
   gated set_task_status tool */
function TaskRow({ r, right, onDone, done }) {
  return (
    <NoteRow note={r.note} target={r.text} className={done ? "task-done" : ""}>
      <button className={"box" + (done ? " on" : "")}
              aria-label="Mark done"
              onClick={(e) => { e.stopPropagation(); if (!done) onDone(r); }} />
      <span className="grow">{r.text}</span>
      {right}
    </NoteRow>
  );
}

/* Region 1 — do. Hard commitments first (due today, overdue, broken
   dates — rows carry the delta, never the bare state), then the plan:
   ⏳-today tasks, with how long each has carried and a "later" defer.
   Then today's meetings. */
function DoToday({ due, planned, meetings, onDone, doneKeys, onDefer, moved }) {
  const rows = [
    ...due.broken.map((r) => ({ ...r, cls: "overdue", when: `due date ${r.due} is not a real date` })),
    ...due.overdue.map((r) => ({ ...r, cls: "overdue", when: `${r.days}d overdue` })),
    ...due.due_today.map((r) => ({ ...r, cls: "due-today", when: "due today" })),
  ];
  if (rows.length === 0 && meetings.length === 0 && planned.length === 0) return null;
  return (
    <Section id="do-today" label="What must be done today?" verb="do">
      <Rows
        items={rows}
        render={(r, i) => (
          <TaskRow key={i} r={r} onDone={onDone}
                   done={doneKeys.has(r.note + "\u0000" + r.text)}
                   right={<span className={`small ${r.cls}`}>{r.when}</span>} />
        )}
      />
      {planned.map((r, i) => (
        <TaskRow key={`p${i}`} r={r} onDone={onDone} done={doneKeys.has(doneKey(r))}
                 right={<>
                   <span className="small ghost">
                     {r.slipped ? `planned · carried ${r.slipped}d` : "planned"}
                   </span>
                   {r.due && <span className="chip">due {r.due}</span>}
                   {moved.has(doneKey(r))
                     ? <span className="chip">pushed</span>
                     : <button className="affordance linklike"
                               onClick={(e) => { e.stopPropagation(); onDefer(r); }}>
                         later →
                       </button>}
                 </>} />
      ))}
      {meetings.map((rel) => (
        <NoteRow note={rel} key={rel}>
          <span className="grow">{noteName(rel)}</span>
          <span className="chip">meeting today</span>
        </NoteRow>
      ))}
    </Section>
  );
}

const weekday = (iso) =>
  new Date(iso + "T00:00:00").toLocaleDateString(undefined, { weekday: "short" });

/* The week tab — the this-week / next-week planning model. Three
   horizons and one promote ladder: long-term queue → next week → this
   week → today. Rows mix hard dues (chip keeps the day: "due Thu") and
   ⏳ plans ("planned" — week-granular on purpose, no false day
   precision). Every move sets ⏳ through the gated verb (a plan, never
   a deadline): one revertible snapshot, one corpus row — the
   promote/defer stream the judge trains on. */
function WeekPane({ today, onDone, doneKeys, moved, onMove }) {
  const { due } = today;
  const mover = (r, when, label) => moved.has(doneKey(r))
    ? <span className="chip verdict-approve">moved</span>
    : <button className="affordance linklike"
              onClick={(e) => { e.stopPropagation(); onMove(r, when, label); }}>
        {label}
      </button>;
  const whenChip = (r) => (
    <span className={"small " + (r.kind === "due" ? "meta" : "ghost")}>
      {r.kind === "due" ? `due ${weekday(r.when)}` : "planned"}
    </span>
  );
  const empty = today.this_week.length + today.next_week.length
    + today.later.length + today.backlog.length === 0;
  return (
    <>
      <header className="header">
        <div className="title">This week</div>
        <div className="date">plan by week — days are for deadlines</div>
      </header>
      {empty && <div className="empty-state title">Nothing on the runway.</div>}
      {today.this_week.length > 0 && (
        <Section id="this-week" label="What lands this week?" verb="do">
          <Rows items={today.this_week} render={(r, i) => (
            <TaskRow key={i} r={r} onDone={onDone} done={doneKeys.has(doneKey(r))}
                     right={<>{whenChip(r)}{mover(r, today.date, "→ today")}</>} />
          )} />
        </Section>
      )}
      {today.next_week.length > 0 && (
        <Section id="next-week" label="What's lined up for next week?" verb="prep">
          <Rows items={today.next_week} render={(r, i) => (
            <TaskRow key={i} r={r} onDone={onDone} done={doneKeys.has(doneKey(r))}
                     right={<>{whenChip(r)}
                            {moved.has(doneKey(r))
                              ? <span className="chip verdict-approve">moved</span>
                              : <>
                                  {mover(r, due.this_sunday, "→ this week")}
                                  {mover(r, today.date, "→ today")}
                                </>}</>} />
          )} />
        </Section>
      )}
      {today.later.length > 0 && (
        <Section id="later" label="Later — has a date, not this fortnight">
          <Rows items={today.later} fold={5} render={(r, i) => (
            <TaskRow key={i} r={r} onDone={onDone} done={doneKeys.has(doneKey(r))}
                     right={<>
                       <span className="small ghost">{r.kind === "due"
                         ? `due ${r.when}` : `planned ${r.when}`}</span>
                       {moved.has(doneKey(r))
                         ? <span className="chip verdict-approve">moved</span>
                         : <>
                             {mover(r, due.this_sunday, "→ this week")}
                             {mover(r, today.date, "→ today")}
                           </>}
                     </>} />
          )} />
        </Section>
      )}
      {today.backlog.length > 0 && (
        <Section id="backlog" label="Long-term queue — no week attached" verb="judge">
          <Rows items={today.backlog} fold={8} label="queued" render={(r, i) => (
            <TaskRow key={i} r={r} onDone={onDone} done={doneKeys.has(doneKey(r))}
                     right={<>
                       {r.priority && <span className="chip">{r.priority}</span>}
                       {r.age_days != null && (
                         <span className="small ghost">{r.age_days}d old</span>
                       )}
                       {moved.has(doneKey(r))
                         ? <span className="chip verdict-approve">moved</span>
                         : <>
                             {mover(r, due.this_sunday, "→ this week")}
                             {mover(r, today.date, "→ today")}
                           </>}
                     </>} />
          )} />
        </Section>
      )}
    </>
  );
}

function StepPicker({ steps, onApprove }) {
  const [checked, setChecked] = useState(() => new Set(steps.map((_, i) => i)));
  const partial = checked.size < steps.length;
  const toggle = (i) => setChecked((c) => {
    const n = new Set(c);
    n.has(i) ? n.delete(i) : n.add(i);
    return n;
  });
  return (
    <div className="steps">
      {steps.map((s, i) => (
        <button className="row step" key={i} onClick={() => toggle(i)}>
          <span className={"box" + (checked.has(i) ? " on" : "")} />
          <span className="grow" style={{ textAlign: "left" }}>{s.label}</span>
          {s.due && <span className="chip">due {s.due}</span>}
        </button>
      ))}
      <button className="decide approve approve-all"
              disabled={checked.size === 0}
              onClick={() => onApprove(partial ? [...checked].sort((a, b) => a - b) : undefined)}>
        {partial ? `Approve selected (${checked.size})` : "Approve all"}
      </button>
    </div>
  );
}

function Proposal({ p, selected, actedVerdict, onDecide, innerRef }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (selected) setOpen(true);
  }, [selected]);
  const decided = actedVerdict != null;
  const hasSteps = p.steps?.length > 1;
  return (
    <Collapsible.Root open={open} onOpenChange={setOpen} asChild>
      <div ref={innerRef}
           className={"proposal" + (selected ? " selected" : "") + (decided ? " decided" : "")}
           data-state={open ? "open" : "closed"}>
        <Collapsible.Trigger asChild>
          <button className="row">
            <span className="caret">▸</span>
            <span className="grow">{p.title}</span>
            {decided ? (
              <span className={`chip verdict-${actedVerdict}`}>
                {actedVerdict === "approve" ? "approved" : "rejected"}
              </span>
            ) : (
              <>
                <span className="decide-buttons">
                  <button className="decide approve"
                          onClick={(e) => { e.stopPropagation(); onDecide(p.id, "approve"); }}>
                    approve
                  </button>
                  <button className="decide reject"
                          onClick={(e) => { e.stopPropagation(); onDecide(p.id, "reject"); }}>
                    reject
                  </button>
                </span>
                <span className="chip">{p.kind}</span>
              </>
            )}
            <span className="small ghost">{noteName(p.note)}</span>
          </button>
        </Collapsible.Trigger>
        <Collapsible.Content className="proposal-body">
          <div className="meta small">{p.summary}</div>
          {hasSteps && !decided
            ? <StepPicker steps={p.steps}
                          onApprove={(steps) => onDecide(p.id, "approve", steps)} />
            : p.issues.length > 0 && (
                <ul className="issues small">
                  {p.issues.map((i, k) => <li key={k}>{i}</li>)}
                </ul>
              )}
          {p.diff_error
            ? <div className="ghost small">{p.diff_error}</div>
            : <Diff rows={p.diff} />}
          {p.not_previewed?.length > 0 && (
            <div className="ghost small">not previewed: {p.not_previewed.join(", ")}</div>
          )}
        </Collapsible.Content>
      </div>
    </Collapsible.Root>
  );
}

/* One autonomous decision under "What changed without you?", expandable
   to its receipt — the detail the canvas used to keep in its own scroll-
   back: what changed, where it landed (opens our editor), and the revert
   that doubles as the negative label. One history, one place. */
function AutoRow({ a, reverted, onRevert }) {
  const openNote = useContext(OpenNote);
  const [open, setOpen] = useState(false);
  return (
    <Collapsible.Root open={open} onOpenChange={setOpen} asChild>
      <div className="proposal" data-state={open ? "open" : "closed"}>
        <Collapsible.Trigger asChild>
          <button className="row">
            <span className="caret">▸</span>
            <span className="grow">
              {a.title}
              {a.unvouched && <span className="chip attention">unvouched</span>}
            </span>
            <span className="small ghost">{a.ts.slice(5, 16).replace("T", " ")}</span>
            {reverted
              ? <span className="chip verdict-reject">reverted</span>
              : a.undo.length > 0 && (
                  <button className="affordance linklike"
                          onClick={(e) => { e.stopPropagation(); onRevert(a); }}>
                    revert ↺
                  </button>
                )}
          </button>
        </Collapsible.Trigger>
        <Collapsible.Content className="proposal-body">
          {a.summary && <div className="meta small">{a.summary}</div>}
          <div className="small" style={{ marginTop: "var(--s2)" }}>
            <span className="chip">{a.kind}</span>{" "}
            <button className="linklike doc-link" onClick={() => openNote(a.note)}>
              → {a.note.replace(/\.md$/, "")}
            </button>
          </div>
        </Collapsible.Content>
      </div>
    </Collapsible.Root>
  );
}

/* Both columns are always mounted now, so "go to X" means scroll it
   into view rather than swap the page. */
function scrollToCol(which) {
  document.getElementById(which)?.scrollTo({ top: 0, behavior: "smooth" });
  if (which === "col-canvas")
    document.querySelector(".canvas-input")?.focus();
}

function usePassTicker(lastPassAt) {
  const [, force] = useState(0);
  useEffect(() => {
    const t = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);
  if (!lastPassAt) return "";
  const s = Math.max(0, Math.floor((Date.now() - lastPassAt.getTime()) / 1000));
  if (s < 60) return `pass ${s}s ago`;
  if (s < 3600) return `pass ${Math.floor(s / 60)}m ago`;
  return `pass ${Math.floor(s / 3600)}h ago`;
}

function Palette({ open, setOpen, today, changes, tree, refetch, runPass,
                   togglePane, toggleExplorer, toggleFocus, openConsole,
                   azurePrefill, revertLast }) {
  /* The notes today points at: the hot set, first, because they're what
     you're most likely typing at. */
  const notes = useMemo(() => {
    if (!today) return [];
    const set = new Map();
    for (const r of [...today.due.overdue, ...today.due.due_today,
                     ...today.due.soon, ...today.waiting, ...today.stranded])
      set.set(r.note, true);
    for (const rel of today.meetings) set.set(rel, true);
    for (const b of today.in_flight) set.set(b.note, true);
    return [...set.keys()];
  }, [today]);

  /* …and then everything else in the vault. The explorer's payload
     doubles as the palette's index: typing a name should always beat
     walking a tree, so the rail stays the deliberate path, not the only
     one.

     Gated on two typed characters and capped, because cmdk renders every
     item it's given and hides the misses — handing it a whole vault
     would make the palette janky on the keystroke that opens it, which
     is the one moment it must feel instant. */
  const [query, setQuery] = useState("");
  const rest = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (q.length < 2) return [];
    const hot = new Set(notes);
    return (tree?.notes ?? [])
      .filter((rel) => !hot.has(rel) && rel.toLowerCase().includes(q))
      .slice(0, 100);
  }, [tree, notes, query]);

  const go = (fn) => () => { fn(); setOpen(false); };
  const openNote = useContext(OpenNote);

  return (
    <Command.Dialog open={open} onOpenChange={setOpen} label="Command palette">
      <Command.Input placeholder="Type a command or search…" autoFocus
                     value={query} onValueChange={setQuery} />
      <Command.List>
        <Command.Empty>Nothing found.</Command.Empty>
        <Command.Group>
          <Command.Item onSelect={go(() => scrollToCol("col-canvas"))}>
            <span>Focus the Canvas</span><span className="ghost small">dump anything</span>
          </Command.Item>
          <Command.Item onSelect={go(() => scrollToCol("col-today"))}>
            <span>Focus the Dashboard</span><span className="ghost small">today</span>
          </Command.Item>
          <Command.Item onSelect={go(togglePane)}>
            <span>Toggle chat pane</span><span className="ghost small">⌘J</span>
          </Command.Item>
          <Command.Item onSelect={go(toggleExplorer)}>
            <span>Toggle file explorer</span><span className="ghost small">⌘B</span>
          </Command.Item>
          <Command.Item onSelect={go(toggleFocus)}>
            <span>Toggle focus mode</span>
            <span className="ghost small">click a task, the rest dims</span>
          </Command.Item>
          <Command.Item onSelect={go(openConsole)}>
            <span>Open the console</span><span className="ghost small">⌘,</span>
          </Command.Item>
          <Command.Item onSelect={go(runPass)}>
            <span>Run an engine pass now</span><span className="ghost small">engine</span>
          </Command.Item>
          <Command.Item onSelect={go(azurePrefill)}>
            <span>//azure — search Microsoft Learn</span><span className="ghost small">canvas</span>
          </Command.Item>
          {changes?.commits?.length > 0 && (
            <Command.Item onSelect={go(revertLast)}>
              <span>Revert last snapshot</span>
              <span className="ghost small">{changes.commits[0].subject.slice(0, 28)}…</span>
            </Command.Item>
          )}
          <Command.Item onSelect={go(refetch)}>
            <span>Refresh</span><span className="ghost small">now</span>
          </Command.Item>
          {["Harness/Capture.md", "Harness/Spec.md"].map((n) => (
            <Command.Item key={n} onSelect={go(() => openNote(n))}>
              <span>Open {noteName(n)}</span><span className="ghost small">↗ editor</span>
            </Command.Item>
          ))}
        </Command.Group>
        {notes.length > 0 && (
          <Command.Group heading="">
            {notes.map((n) => (
              <Command.Item key={n} onSelect={go(() => openNote(n))}>
                <span>{noteName(n)}</span><span className="ghost small">{n}</span>
              </Command.Item>
            ))}
          </Command.Group>
        )}
        {rest.length > 0 && (
          <Command.Group heading="">
            {rest.map((n) => (
              <Command.Item key={n} value={n} onSelect={go(() => openNote(n))}>
                <span>{noteName(n)}</span><span className="ghost small">{n}</span>
              </Command.Item>
            ))}
          </Command.Group>
        )}
      </Command.List>
    </Command.Dialog>
  );
}

export default function App() {
  const { today, review, changes, health, tree, error, live, lastPassAt, activity,
          workerError, clearWorkerError, refetch } = useDashboard();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [consoleOpen, setConsoleOpen] = useState(false);
  const [paneOpen, setPaneOpen] = useState(false);
  // Closed unless you've said otherwise: the dashboard is the surface,
  // the rail is the escape hatch for "I know where that note lives".
  const [explorerOpen, setExplorerOpen] = useState(
    () => localStorage.getItem("tezcat.explorer") === "1");
  const [frac, setFrac] = useSplit("tezcat.split");
  const splitRef = useRef(null);
  const [acted, setActed] = useState({});          // pid -> "approve" | "reject"
  const [toast, setToast] = useState(null);        // {label, undo: [shas]}
  const [selectedPid, setSelectedPid] = useState(null);
  // The clicked task row (note + text key). Feeds the row highlight and,
  // when focus mode is on, the dimming of everything else. Esc clears.
  const [selKey, setSelKey] = useState(null);
  const [focusMode, setFocusMode] = useState(
    () => localStorage.getItem("tezcat.focus") === "1");
  const [movedKeys, setMovedKeys] = useState(() => new Set()); // optimistic ⏳ moves (promote/defer, any tab)
  const [doneKeys, setDoneKeys] = useState(new Set());
  const [canvasSeed, setCanvasSeed] = useState("");
  // Which note is open, and which of the two the right column is showing.
  // Kept apart so switching to the canvas doesn't discard the note (or its
  // unsaved edits) — still one note at a time; opening another replaces it.
  const [openNote, setOpenNote] = useState(null);   // {path, target, seq} | null
  const [tab, setTab] = useState("canvas");         // "canvas" | "note"

  const openNoteAt = useCallback((path, target = "") => {
    setOpenNote((prev) => ({ path, target, seq: (prev?.seq ?? 0) + 1 }));
    setTab("note");
  }, []);
  // Closing the note takes the selection with it: the editor half of the
  // highlight just vanished, and a lit row pointing at nothing is a lie.
  const closeNote = useCallback(() => {
    setOpenNote(null);
    setTab("canvas");
    setSelKey(null);
  }, []);
  const rowRefs = useRef({});
  const toastTimer = useRef(null);
  const ticker = usePassTicker(lastPassAt);

  // The decision region owns every pending proposal now — the canvas
  // keeps no inline decide boxes, so nothing needs de-duplicating.
  const visible = useMemo(() => review ?? [], [review]);
  /* Releasing the selection releases BOTH sides: the editor's line mark
     reads the open note's target, so blank it along with the row key —
     the highlight over the canvas column must never outlive (or die
     before) the lit row that put it there. */
  const releaseSel = useCallback(() => {
    setSelKey(null);
    setOpenNote((prev) => (prev?.target ? { ...prev, target: "" } : prev));
  }, []);
  const selCtx = useMemo(() => ({
    key: selKey,
    set: (k) => (k === null ? releaseSel() : setSelKey(k)),
  }), [selKey, releaseSel]);
  const toggleFocus = useCallback(() => setFocusMode((v) => {
    localStorage.setItem("tezcat.focus", v ? "0" : "1");
    return !v;
  }), []);

  const showToast = useCallback((label, undo = []) => {
    setToast({ label, undo });
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 5500);
  }, []);

  /* Optimistic, but only optimistic about things that CAN happen: the
     server 409s when a decision can't take the pass lock, so a lost
     verdict lands in the catch below instead of leaving the row marked
     decided over a change that never applied. Any throw drops the mark. */
  const decide = useCallback(async (pid, verdict, steps) => {
    setActed((a) => ({ ...a, [pid]: verdict }));   // optimistic, instantly
    try {
      const res = await postJSON("/api/decide",
        { pid, verdict, ...(steps ? { steps } : {}) });
      const s = res.summary;
      const outcome = s.approved
        ? (steps ? `Approved ${steps.length} of the steps` : "Approved")
        : s.rejected ? "Rejected"
        : s.stale ? "Stale — the note moved since drafting" : "Failed to apply";
      showToast(outcome + " — snapshot kept",
                verdict === "approve" ? res.undo : []);
    } catch (e) {
      setActed((a) => { const { [pid]: _gone, ...rest } = a; return rest; });
      showToast(String(e.message || e));
      refetch(["review"]);   // whatever happened, the queue is the truth
    }
  }, [showToast, refetch]);

  const undo = useCallback(async () => {
    if (!toast?.undo?.length) return;
    const shas = toast.undo;
    setToast(null);
    try {
      for (const sha of shas) await postJSON("/api/revert", { sha });
      showToast("Undone — reverted as a new snapshot");
    } catch (e) {
      showToast(`Undo failed: ${e.message || e}`);
    }
    refetch();
  }, [toast, refetch, showToast]);

  // Reverting an autonomous decision = reverting its own commits, in
  // order. The revert is the negative label the autonomous mode trains
  // on — this button existing is what makes "revert, don't hand-fix"
  // a reasonable ask.
  const [revertedAuto, setRevertedAuto] = useState(() => new Set());
  const revertAuto = useCallback(async (a) => {
    setRevertedAuto((s) => new Set(s).add(a.id));
    try {
      for (const sha of a.undo) await postJSON("/api/revert", { sha });
      showToast("Reverted — recorded as a negative label");
    } catch (e) {
      setRevertedAuto((s) => { const n = new Set(s); n.delete(a.id); return n; });
      showToast(`Revert failed: ${e.message || e}`);
    }
    refetch();
  }, [refetch, showToast]);

  const revertCommit = useCallback(async (sha) => {
    try {
      const res = await postJSON("/api/revert", { sha });
      showToast(`Reverted “${res.reverted}” — snapshot kept`);
    } catch (e) {
      showToast(`Revert failed: ${e.message || e}`);
    }
    refetch();
  }, [refetch, showToast]);

  const taskDone = useCallback(async (r) => {
    const key = r.note + "\u0000" + r.text;
    setDoneKeys((k) => new Set(k).add(key));       // optimistic strike
    try {
      await postJSON("/api/task/done", { note: r.note, text: r.text });
      showToast("Done — snapshot kept");
    } catch (e) {
      setDoneKeys((k) => { const n = new Set(k); n.delete(key); return n; });
      showToast(`Couldn’t check it off: ${e.message || e}`);
    }
  }, [showToast]);

  /* Promote/defer move the ⏳ plan date, NEVER 📅 — most tasks have no
     real deadline, and fabricating one would read "overdue" tomorrow:
     manufactured urgency. Every move is corpus-recorded (promote vs
     defer) — the stream a future triage worker trains on. */
  const planTask = useCallback(async (r, when, label) => {
    const key = doneKey(r);
    setMovedKeys((k) => new Set(k).add(key));       // optimistic, instantly
    try {
      await postJSON("/api/task/plan", { note: r.note, text: r.text, when });
      showToast(`${label} — snapshot kept`);
    } catch (e) {
      setMovedKeys((k) => { const n = new Set(k); n.delete(key); return n; });
      showToast(`Couldn’t move it: ${e.message || e}`);
    }
  }, [showToast]);
  // Defer from Today = the rest of this week; on Sunday there is no
  // rest, so it rolls to next week's anchor.
  const deferTask = useCallback((r) => {
    const when = today?.due?.this_sunday !== today?.date
      ? today?.due?.this_sunday : today?.due?.next_sunday;
    return planTask(r, when, "Pushed to later this week");
  }, [planTask, today]);
  /* "→ today" and "→ this week" from every row a task lives on — soon,
     stranded, the week lanes, the backlog — not just the Week tab. Same
     gated ⏳ verb (plan, never deadline), same corpus row, wherever it's
     clicked. Both verbs travel together: deciding a task isn't for today
     is the same moment as deciding when it IS for, and sending the user
     to another tab to say so is how things end up undated. */
  const planBtns = useCallback((r) => {
    if (movedKeys.has(doneKey(r)))
      return <span className="chip verdict-approve">moved</span>;
    const plan = (when, label) => (e) => { e.stopPropagation(); planTask(r, when, label); };
    return (
      <>
        <button className="affordance linklike"
                onClick={plan(today?.date, "Promoted to today")}>
          → today
        </button>
        {/* On a Sunday the week anchor IS today; one button, not two
            spellings of it. */}
        {today?.due?.this_sunday !== today?.date && (
          <button className="affordance linklike"
                  onClick={plan(today?.due?.this_sunday, "Planned for this week")}>
            → this week
          </button>
        )}
      </>
    );
  }, [movedKeys, planTask, today]);

  /* The chips inside the note editor plan against the SAME anchors the
     rows do — derived once here, never recomputed from a Date() down
     there, so the two surfaces can't disagree about which Sunday it is. */
  const planDates = useMemo(
    () => ({ today: today?.date ?? "", week: today?.due?.this_sunday ?? "" }),
    [today?.date, today?.due?.this_sunday]);

  const runPass = useCallback(() => { postJSON("/api/pass").catch(() => {}); }, []);
  const stopPass = useCallback(() => { cancelJob("pass").catch(() => {}); }, []);
  const togglePane = useCallback(() => setPaneOpen((v) => !v), []);
  const toggleExplorer = useCallback(() => setExplorerOpen((v) => {
    localStorage.setItem("tezcat.explorer", v ? "0" : "1");
    return !v;
  }), []);
  const azurePrefill = useCallback(() => {
    setCanvasSeed("//azure ");
    scrollToCol("col-canvas");
  }, []);

  const onDrag = useCallback((clientX) => {
    const el = splitRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setFrac((clientX - r.left) / r.width);
  }, [setFrac]);
  const revertLast = useCallback(() => {
    if (changes?.commits?.length) revertCommit(changes.commits[0].sha);
  }, [changes, revertCommit]);

  /* keyboard-first review: twenty seconds, zero mouse */
  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.key === "j") {
        e.preventDefault();
        togglePane();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.key === "b") {
        e.preventDefault();
        toggleExplorer();
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.key === ",") {
        e.preventDefault();
        setConsoleOpen((v) => !v);
        return;
      }
      // The console owns the whole screen while it is up: its own fields
      // take the keyboard, and esc there closes it, not the row selection.
      if (consoleOpen) return;
      if (paletteOpen || e.metaKey || e.ctrlKey || e.altKey) return;
      // esc releases the clicked row (and focus mode's dim) everywhere,
      // editor mark included
      if (e.key === "Escape") releaseSel();
      // the review shortcuts belong to Today's decision region — which
      // is pinned now, so they are always live
      // the canvas textarea is always mounted now: never steal its keys
      if (/^(input|textarea|select)$/i.test(e.target.tagName)) return;
      // nor the explorer's: its rows are buttons, and `e` on a focused
      // tree row must not fire the review shortcut for a different note
      if (e.target.closest?.(".explorer")) return;
      const pending = visible.filter((p) => !(p.id in acted));
      const idx = pending.findIndex((p) => p.id === selectedPid);
      const move = (d) => {
        const next = pending[Math.min(Math.max(idx + d, 0), pending.length - 1)];
        if (next) {
          setSelectedPid(next.id);
          rowRefs.current[next.id]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
        }
      };
      if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "a" && selectedPid && idx >= 0) decide(selectedPid, "approve");
      else if (e.key === "r" && selectedPid && idx >= 0) decide(selectedPid, "reject");
      else if (e.key === "e" && selectedPid && idx >= 0) {
        const p = pending[idx];
        if (p) openNoteAt(p.note);   // our editor now, not a deep link out
      }
      else if (e.key === "u") undo();
      else if (e.key === "Escape") setSelectedPid(null);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [paletteOpen, visible, acted, selectedPid, decide, undo, openNoteAt,
      togglePane, toggleExplorer, releaseSel, consoleOpen]);

  /* refetch reconciles: decided proposals leave the queue, done rows
     leave the model; drop the optimistic marks so state never rots */
  useEffect(() => {
    if (!review) return;
    const ids = new Set(review.map((p) => p.id));
    setActed((a) => Object.fromEntries(Object.entries(a).filter(([pid]) => ids.has(pid))));
    if (selectedPid && !ids.has(selectedPid)) setSelectedPid(null);
  }, [review, selectedPid]);
  useEffect(() => {
    if (!today) return;
    const live = new Set();
    for (const r of [...today.due.overdue, ...today.due.due_today,
                     ...today.due.soon, ...today.due.broken, ...today.waiting])
      live.add(r.note + "\u0000" + r.text);
    setDoneKeys((k) => new Set([...k].filter((key) => live.has(key))));
    // moved rows leave the week buckets once the refetch lands; pruning
    // by liveness (not clearing) keeps the "moved" chip up during the
    // gap between optimistic mark and fresh data
    const moveLive = new Set([...today.due.soon,
                              ...(today.this_week ?? []),
                              ...(today.next_week ?? []),
                              ...(today.later ?? []),
                              ...(today.planned ?? []),
                              ...(today.backlog ?? [])].map(doneKey));
    setMovedKeys((k) => new Set([...k].filter((key) => moveLive.has(key))));
  }, [today]);

  if (error && !today)
    return <div className="page"><div className="ghost">Engine unreachable — the vault still works from the editor. ({error})</div></div>;
  if (!today) return <div className="page" />;

  const dateStr = new Date(today.date + "T00:00:00").toLocaleDateString(undefined,
    { weekday: "long", month: "long", day: "numeric" });
  const pending = visible.filter((p) => !(p.id in acted)).length;
  const nothingDue = today.due.broken.length + today.due.overdue.length
    + today.due.due_today.length + today.due.soon.length === 0;
  // The empty state is the success condition, and Reference content
  // (work in flight, history) never blocks it — only the four question
  // regions count.
  const nothing = pending === 0 && nothingDue && today.waiting.length === 0
    && today.stranded.length === 0 && today.meetings.length === 0
    && (today.planned ?? []).length === 0;

  return (
    <OpenNote.Provider value={openNoteAt}>
    <SelectRow.Provider value={selCtx}>
    <div className={"shell" + (focusMode && selKey ? " focus" : "")}>
      <Console open={consoleOpen} onClose={() => setConsoleOpen(false)} />
      <nav className="topnav small">
        <span className="brand">tezcat</span>
        <span className="nav-spacer" />
        <Activity activity={activity} onStop={stopPass} />
        <span className="ghost">{live ? ticker : "reconnecting"}</span>
        <button className={"navlink" + (focusMode ? " current" : "")}
                onClick={toggleFocus}
                title="Focus mode: click a task and the rest dims — esc releases">
          Focus
        </button>
        <button className="navlink" onClick={toggleExplorer}>Files ⌘B</button>
        <button className="navlink" onClick={togglePane}>Chat ⌘J</button>
        <button className="navlink" onClick={() => setConsoleOpen(true)}
                title="Everything configurable: vault behaviour and models">
          Console ⌘,
        </button>
      </nav>

      {/* The rail sits OUTSIDE the split so opening it never touches the
          fraction you dragged between the dashboard and the canvas. */}
      <div className="body">
      <Explorer tree={tree} open={explorerOpen} onToggle={toggleExplorer}
                active={openNote?.path ?? null} onOpen={openNoteAt} />

      <div className="split" ref={splitRef}
           style={{ gridTemplateColumns: `${frac}fr 6px ${1 - frac}fr` }}>
      <div className="col" id="col-today">
      {/* Today is PINNED — the one always-on surface, never behind a
          tab. Week (the runway) lives in the right column's tabs beside
          the canvas: two horizons over one model, side by side instead
          of one replacing the other. */}
      <>
      <header className="header">
        <div className="title">Today</div>
        <div className="date">{dateStr}</div>
        {pending > 0 && (
          <div className="needs-you" style={{ marginTop: "var(--s2)" }}>
            {pending} decision{pending > 1 ? "s" : ""} need{pending > 1 ? "" : "s"} you
          </div>
        )}
      </header>

      {/* A draft worker that never answered. Above the empty state on
          purpose: "Nothing needs you" is only the success condition when
          the harness actually looked — with extraction down we don't know
          what's in the vault, and saying nothing would be the same lie the
          canvas used to tell ("nothing extractable"). Dumps are safe on
          Capture.md and re-arm; this is a delay with a cause. */}
      {workerError && (
        <div className="worker-down small" role="status">
          <span className="attention">extraction is down</span>
          <span className="ghost">
            {" "}— {workerError.processor} couldn’t draft; captures stay on
            Capture.md{workerError.retrying === false
              ? " — retried and parked, so edit the text to try again"
              : " and retry each pass"}
          </span>
          <div className="ghost">{workerError.error}</div>
          <button className="linklike" onClick={clearWorkerError}>dismiss</button>
        </div>
      )}

      {nothing && !workerError && <div className="empty-state title">Nothing needs you.</div>}

      <DoToday due={today.due} planned={today.planned ?? []}
               meetings={today.meetings} onDone={taskDone} doneKeys={doneKeys}
               onDefer={deferTask} moved={movedKeys} />

      {today.due.soon.length > 0 && (
        <Section id="soon" label="What lands soon?" verb="start">
          <Rows items={today.due.soon} render={(r, i) => (
            <TaskRow key={i} r={r} onDone={taskDone} done={doneKeys.has(doneKey(r))}
                     right={<>
                       <span className="small meta">in {r.days}d</span>
                       {planBtns(r)}
                     </>} />
          )} />
        </Section>
      )}

      {today.waiting.length > 0 && (
        <Section id="stalled" label="What’s stalled on someone else?" verb="nudge">
          {/* Oldest first: the longer it has sat, the harder to ignore —
              the row's action is a nudge, and age is the nudge's case. */}
          <Rows items={[...today.waiting].sort(
                  (a, b) => (b.waiting_days ?? 0) - (a.waiting_days ?? 0))}
                render={(r, i) => (
            <TaskRow key={i} r={r} onDone={taskDone}
                     done={doneKeys.has(r.note + "\u0000" + r.text)}
                     right={<>
                       {r.owner && <span className="chip">👤 {r.owner}</span>}
                       {r.due && <span className="chip">due {r.due}</span>}
                       {r.waiting_days >= 3 && <span className="chip attention">{r.waiting_days}d</span>}
                     </>} />
          )} />
        </Section>
      )}

      {(visible.length > 0 || today.stranded.length > 0) && (
        <Section id="review" label="What needs a decision?" verb="decide">
          {visible.map((p) => (
            <Proposal key={p.id} p={p}
                      selected={p.id === selectedPid}
                      actedVerdict={acted[p.id]}
                      onDecide={decide}
                      innerRef={(el) => { rowRefs.current[p.id] = el; }} />
          ))}
          {visible.length > 0 && (
            <div className="ghost small" style={{ marginTop: "var(--s2)" }}>
              j/k select · a approve · r reject · e edit · u undo
            </div>
          )}
          {/* Undated tasks rotting in old records — meetings, captures,
              anywhere: captured but never dispositioned — the decision is
              to date it, do it, or delete it. Clicking opens the note on
              the line. */}
          <Rows items={today.stranded} fold={5} render={(r, i) => (
            <NoteRow note={r.note} target={r.text} key={i}>
              <span className="grow">{r.text}</span>
              <span className="small ghost">{r.age_days}d old · undated</span>
              {planBtns(r)}
            </NoteRow>
          )} />
        </Section>
      )}

      {/* The autonomy audit: under the standing grant, "what did it
          change without asking?" is a real question with a real action
          — revert, which is also the training label. One row per
          autonomous decision, expandable to its receipt (summary, kind,
          the note it landed on), revertible as a decision (its own
          commits), "unvouched" marking applies the verifier objected
          to. This is where canvas dumps land too — the one history.
          Absent entirely when the harness has been quiet. */}
      {(today.recent_auto ?? []).length > 0 && (
        <Section id="auto" label="What changed without you?" verb="audit">
          {today.recent_auto.map((a) => (
            <AutoRow key={a.id} a={a}
                     reverted={revertedAuto.has(a.id)} onRevert={revertAuto} />
          ))}
        </Section>
      )}

      {/* Below the fold on purpose: nothing here answers a question or
          carries an action — it's evidence (work in flight, the audit
          trail). Folded so reference can never crowd out the four
          regions above, and closed by default so the empty state stays
          reachable. */}
      {(today.in_flight.length > 0 || changes?.commits?.length > 0) && (
        <Collapsible.Root asChild>
          <section className="reference">
            <Collapsible.Trigger asChild>
              <button className="row reference-toggle">
                <span className="caret">▸</span>
                <span className="grow small">Reference</span>
                <span className="ghost small">
                  {today.in_flight.length > 0 && `${today.in_flight.length} board(s) in flight · `}
                  history
                </span>
              </button>
            </Collapsible.Trigger>
            <Collapsible.Content>
              {today.in_flight.length > 0 && (
                <Section id="inflight" label="In flight">
                  {today.in_flight.map((b, i) => (
                    <NoteRow note={b.note} key={i}>
                      <span className="grow">
                        {noteName(b.note)} <span className="meta">· {b.column}:</span>{" "}
                        {b.cards.slice(0, 5).join(", ")}
                        {b.cards.length > 5 && <span className="ghost"> +{b.cards.length - 5}</span>}
                      </span>
                    </NoteRow>
                  ))}
                </Section>
              )}
              {changes?.commits?.length > 0 && (
                <Section id="changes" label="History — each row is one revertible snapshot">
                  <Rows items={changes.commits} fold={6} label="commits" render={(c) => (
                    <div className="row commit" key={c.sha}>
                      <span className="grow">
                        {c.subject}
                        <span className="files"> {c.files.slice(0, 3).join(", ")}
                          {c.files.length > 3 && ` +${c.files.length - 3}`}</span>
                      </span>
                      <span className="small ghost">{c.date.slice(5, 16).replace("T", " ")}</span>
                      <button className="affordance linklike" onClick={() => revertCommit(c.sha)}>
                        revert ↺
                      </button>
                    </div>
                  )} />
                </Section>
              )}
            </Collapsible.Content>
          </section>
        </Collapsible.Root>
      )}
      </>
      </div>

      <Splitter onDrag={onDrag} />

      <div className="col" id="col-canvas">
        <div className="tabs small">
          <button className={"tab" + (tab === "canvas" ? " current" : "")}
                  onClick={() => setTab("canvas")}>Canvas</button>
          <button className={"tab" + (tab === "week" ? " current" : "")}
                  onClick={() => setTab("week")}>Week</button>
          {openNote && (
            <button className={"tab" + (tab === "note" ? " current" : "")}
                    title={openNote.path} onClick={() => setTab("note")}>
              {noteName(openNote.path)}
            </button>
          )}
        </div>

        {/* Both stay mounted: the canvas keeps its dump history, and the
            editor keeps unsaved edits, so switching tabs costs nothing.
            Hidden by class, not inline style — the slot's flex layout is
            what pins the compose box, and an inline display would win. */}
        <div className={"pane-slot" + (tab === "canvas" ? "" : " hidden")}>
          {/* no title here: the tab above already says Canvas */}
          <header className="header">
            <div className="date">
              Dump anything. The harness extracts, proposes, and files —
              nothing lands without you.
            </div>
          </header>
          <Canvas seed={canvasSeed}
                  onSeedConsumed={() => setCanvasSeed("")}
                  onIngested={() => refetch(["today", "review", "changes"])} />
        </div>

        <div className={"pane-slot" + (tab === "week" ? "" : " hidden")}>
          <div className="pane-scroll">
            <WeekPane today={today} onDone={taskDone}
                      doneKeys={doneKeys} moved={movedKeys} onMove={planTask} />
          </div>
        </div>

        {openNote && (
          <div className={"pane-slot" + (tab === "note" ? "" : " hidden")}>
            <Suspense fallback={<div className="ghost small">opening…</div>}>
              <NoteEditor path={openNote.path} target={openNote.target}
                          seq={openNote.seq} visible={tab === "note"}
                          planDates={planDates} onClose={closeNote} />
            </Suspense>
          </div>
        )}
      </div>
      </div>
      </div>

      <footer className="footer">
        <span><span className={`dot ${live ? "ok" : ""}`} />{live ? "live" : "reconnecting"}</span>
        {health && (
          <span>
            <span className={`dot ${health.worker.reachable ? "ok" : "attention"}`} />
            {health.worker.model || "worker"}
          </span>
        )}
        <span className="ghost">⌘K · ⌘J · ⌘B</span>
      </footer>

      {toast && (
        <div className="toast show" role="status">
          <span>{toast.label}</span>
          {toast.undo?.length > 0 && (
            <button className="linklike toast-undo" onClick={undo}>Undo</button>
          )}
        </div>
      )}

      <Pane open={paneOpen} onClose={() => setPaneOpen(false)}
            onOpenNote={openNoteAt} />

      <Palette open={paletteOpen} setOpen={setPaletteOpen}
               today={today} changes={changes} tree={tree}
               refetch={refetch} runPass={runPass} togglePane={togglePane}
               toggleExplorer={toggleExplorer} toggleFocus={toggleFocus}
               openConsole={() => setConsoleOpen(true)}
               azurePrefill={azurePrefill} revertLast={revertLast} />
    </div>
    </SelectRow.Provider>
    </OpenNote.Provider>
  );
}
