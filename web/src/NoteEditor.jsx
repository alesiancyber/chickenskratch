import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";
import { EditorState } from "@codemirror/state";
import { EditorView, keymap, placeholder } from "@codemirror/view";
import { useCallback, useEffect, useRef, useState } from "react";
import { getJSON, postJSON } from "./api.js";
import {
  calloutField, livePreview, mermaidField, planDatesField, setPlanDates, setTarget,
  targetField, vaultTheme, wikilinkClick,
} from "./livepreview.js";

/* The owned note editor. CodeMirror 6, because the vault is portable
   markdown and the document model here IS the text — no parse, no
   serialize, so a save is byte-identical to what you typed. That is
   load-bearing, not a nicety: is_stale() and verify_schema_fix() both
   compare bytes, so a normalizing editor would stale every pending
   proposal just by opening a note.

   Saving is explicit (⌘S). No autosave: every write is a git snapshot,
   and autosave would turn one sitting into fifty commits in Changes.md.
   The engine may auto-apply to this note while it's open, so saves
   carry the hash they loaded and the server refuses a stale write
   rather than eating the change that landed. */

function statusLine(state, ephemeral) {
  if (state === "conflict") return "changed on disk — reload to continue";
  if (state === "saving") return "saving…";
  if (state === "error") return "save failed";
  if (state === "dirty") return "unsaved · ⌘S";
  if (ephemeral) return "saved · machine-managed surface — the engine may clear it";
  return "saved";
}

/* ⌘↩ = submit to the harness, everywhere. The canvas taught the hand;
   here it runs the //command line under the caret through the same
   executor the pass uses (/api/command) and splices the callout into
   the buffer — a DOCUMENT edit, so ⌘S is still the one write and the
   file is never changed under the open buffer. */

export default function NoteEditor({ path, target, seq, planDates, visible = true,
                                     refresh = 0, onOpenNote, onClose }) {
  const host = useRef(null);
  const view = useRef(null);
  // The editor is built once; wikilink clicks read the CURRENT handler.
  const openNoteRef = useRef(onOpenNote);
  openNoteRef.current = onOpenNote;
  const baseHash = useRef("");
  const saveRef = useRef(() => {});
  const runLineRef = useRef(() => {});
  const [state, setState] = useState("loading");   // loading|clean|dirty|saving|error|conflict
  // Mirrors `state` for the update listener, which is built once with
  // the editor and must read the CURRENT save state, not a stale closure.
  const stateRef = useRef(state);
  stateRef.current = state;
  const [ephemeral, setEphemeral] = useState(false);
  const [cmdBusy, setCmdBusy] = useState(false);   // a ⌘↩ command in flight
  const [error, setError] = useState("");

  const save = useCallback(async () => {
    const v = view.current;
    if (!v) return;
    setState((s) => (s === "conflict" ? s : "saving"));
    try {
      const res = await postJSON("/api/note", {
        path, text: v.state.doc.toString(), hash: baseHash.current,
      });
      baseHash.current = res.hash;
      setEphemeral(Boolean(res.ephemeral));
      setState("clean");
    } catch (e) {
      const msg = String(e.message || e);
      // 409 from the server: the engine (or another editor) got here first
      setState(/changed on disk/.test(msg) ? "conflict" : "error");
      setError(msg);
    }
  }, [path]);
  saveRef.current = save;

  /* Run the //command on `line` and replace it with the returned
     callout. The document is live under the round trip, so the splice
     only lands if the line still says what we sent (the plan-chips
     stale-guard); a keystroke that landed meanwhile owns the line. */
  const runLine = useCallback(async (line) => {
    setCmdBusy(true);
    setError("");
    try {
      const { callout } = await postJSON("/api/command", { line: line.text });
      const v = view.current;
      if (v) {
        const now = v.state.doc.lineAt(Math.min(line.from, v.state.doc.length));
        if (now.text === line.text)
          v.dispatch({ changes: { from: now.from, to: now.to, insert: callout } });
      }
    } catch (e) {
      setError(String(e.message || e));   // shown in the banner; save state untouched
    }
    setCmdBusy(false);
  }, []);
  runLineRef.current = runLine;

  /* Scroll the target into view and mark it. Two kinds arrive: a
     dashboard row passes its task text (a substring of the raw line
     "- [ ] Sam: … ^t-abc", so no re-parsing needed), and a chat citation
     passes the line number it drew from. The mark is the editor half of
     the clicked row's selection — it HOLDS until the selection releases
     (target goes blank: esc, a second click, closing the note) or moves
     to another row, so both sides stay lit together. A target that can't
     be found clears instead of leaving a stale mark from the last note. */
  const reveal = useCallback(() => {
    const v = view.current;
    if (!v) return;
    const clear = () => v.dispatch({ effects: setTarget.of(null) });
    if (!target) return clear();
    const doc = v.state.doc;
    let at;
    if (/^\d+$/.test(target)) {
      const n = Number(target);
      if (n < 1 || n > doc.lines) return clear();
      at = doc.line(n).from;
    } else {
      at = doc.toString().indexOf(target);
      if (at < 0) return clear();
    }
    const line = doc.lineAt(at);
    v.dispatch({
      effects: [
        setTarget.of(line.from),
        EditorView.scrollIntoView(line.from, { y: "center" }),
      ],
    });
  }, [target]);
  const revealRef = useRef(reveal);
  revealRef.current = reveal;

  const reload = useCallback(async () => {
    setState("loading");
    setError("");
    try {
      const data = await getJSON(`/api/note?path=${encodeURIComponent(path)}`);
      baseHash.current = data.hash;
      const v = view.current;
      if (v)
        v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: data.text } });
      setState("clean");
      revealRef.current();   // via ref: reload must not re-run when the target changes
    } catch (e) {
      setState("error");
      setError(String(e.message || e));
    }
  }, [path]);

  /* Hot-sync: a write landed on this note from OUTSIDE the buffer — a
     task checked on Today, an approved proposal, a revert, an engine
     apply. A clean buffer takes the disk version, so the dashboard's
     checkmark and the editor's can never disagree; a dirty buffer is
     the user's and stays theirs (the save-time hash guard remains the
     arbiter). The splice is minimal — common prefix/suffix trimmed —
     so a checked box is a one-character change that moves neither the
     caret nor the scroll. Our own saves echo back through the same
     event; they no-op on the hash. */
  const syncFromDisk = useCallback(async () => {
    if (stateRef.current !== "clean") return;
    try {
      const data = await getJSON(`/api/note?path=${encodeURIComponent(path)}`);
      const v = view.current;
      if (!v || data.hash === baseHash.current || stateRef.current !== "clean") return;
      const old = v.state.doc.toString();
      let from = 0;
      const next = data.text;
      while (from < old.length && from < next.length && old[from] === next[from]) from++;
      let oldTo = old.length, newTo = next.length;
      while (oldTo > from && newTo > from && old[oldTo - 1] === next[newTo - 1]) {
        oldTo--; newTo--;
      }
      baseHash.current = data.hash;
      v.dispatch({ changes: { from, to: oldTo, insert: next.slice(from, newTo) } });
      setState("clean");   // the dispatch above marked the buffer dirty
    } catch { /* transient read failure: the buffer stands */ }
  }, [path]);
  useEffect(() => {
    if (refresh) syncFromDisk();
  }, [refresh, syncFromDisk]);

  /* One EditorView for the component's life; switching notes replaces
     the document rather than tearing the editor down, so scroll position
     and focus behave and we don't churn DOM. */
  useEffect(() => {
    const v = new EditorView({
      parent: host.current,
      state: EditorState.create({
        doc: "",
        extensions: [
          history(),
          keymap.of([
            { key: "Mod-s", preventDefault: true, run: () => { saveRef.current(); return true; } },
            // ⌘↩ on a //command line runs it now; any other line falls
            // through to the default keymap untouched
            { key: "Mod-Enter", run: (view) => {
                const line = view.state.doc.lineAt(view.state.selection.main.head);
                if (!/^\/\//.test(line.text)) return false;
                runLineRef.current(line);
                return true;
              } },
            // Tab indents the line (a subtask is an indented task),
            // Shift-Tab outdents; Escape then Tab leaves the editor
            indentWithTab,
            ...defaultKeymap, ...historyKeymap,
          ]),
          markdown(),
          livePreview,
          wikilinkClick((p, frag) => openNoteRef.current?.(p, frag)),
          targetField,
          planDatesField,
          calloutField,
          mermaidField,
          vaultTheme,
          EditorView.lineWrapping,
          placeholder("Empty note."),
          EditorView.updateListener.of((u) => {
            if (!u.docChanged) return;
            /* A widget affordance (task checkbox, plan chip) is the
               dashboard's verb, not a keystroke: save NOW, dirty buffer
               or not. It used to save only a clean buffer, which parked
               the verb behind an unsaved ⌘S whenever typing preceded
               the click — "→ this week" visibly did nothing until some
               later save, the exact inconsistency a verb must not have.
               Unsaved typing simply rides along: it's the user's own
               words in their own note, the write is snapshot-bracketed,
               and the hash check still catches a note that moved on
               disk (conflict banner, nothing clobbered). */
            if (stateRef.current !== "conflict"
                && u.transactions.some((tr) => tr.isUserEvent("affordance"))) {
              saveRef.current();
              return;
            }
            setState((s) => (s === "conflict" ? s : "dirty"));
          }),
        ],
      }),
    });
    view.current = v;
    return () => { v.destroy(); view.current = null; };
  }, []);

  useEffect(() => { if (path) reload(); }, [path, reload]);

  /* The plan chips need today's date and the week's Sunday anchor. They
     come from the dashboard's model rather than a Date() here, so the
     two surfaces can never disagree about which Sunday "this week" is. */
  useEffect(() => {
    if (planDates) view.current?.dispatch({ effects: setPlanDates.of(planDates) });
  }, [planDates]);

  /* Re-reveal when the dashboard points at a different row of the SAME
     note (`seq` also changes on a repeat click of the same row). Runs on
     a blanked target too — that's the selection releasing, and reveal
     clears the mark so the two sides let go together. */
  useEffect(() => { revealRef.current(); }, [target, seq]);

  /* The editor stays mounted while the canvas is showing — switching
     tabs must not discard unsaved edits — but CodeMirror measures itself
     against a laid-out DOM, so it needs a nudge when it comes back. */
  useEffect(() => {
    if (visible) view.current?.requestMeasure();
  }, [visible]);

  const dirty = state === "dirty";

  return (
    <div className="editor">
      <div className="editor-head">
        <span className="editor-path mono">{path}</span>
        <span className="spacer" />
        <span className={"small " + (state === "conflict" || state === "error"
          ? "attention" : "ghost")}>
          {cmdBusy ? "running //command…" : statusLine(state, ephemeral)}
        </span>
        {state === "conflict"
          ? <button className="decide approve" onClick={reload}>Reload</button>
          : <button className="decide approve" onClick={save} disabled={!dirty}>Save</button>}
        <button className="pane-close" onClick={onClose} aria-label="Close editor">✕</button>
      </div>
      {error && state !== "conflict" && (
        <div className="editor-error small">{error}</div>
      )}
      <div className="editor-body" ref={host} />
    </div>
  );
}
