import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";
import { EditorState } from "@codemirror/state";
import { EditorView, keymap, placeholder } from "@codemirror/view";
import { useCallback, useEffect, useRef, useState } from "react";
import { postJSON } from "./api.js";
import {
  livePreview, planDatesField, setPlanDates, setTarget, targetField, vaultTheme,
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

export default function NoteEditor({ path, target, seq, planDates, visible = true,
                                     onClose }) {
  const host = useRef(null);
  const view = useRef(null);
  const baseHash = useRef("");
  const saveRef = useRef(() => {});
  const [state, setState] = useState("loading");   // loading|clean|dirty|saving|error|conflict
  const [ephemeral, setEphemeral] = useState(false);
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
      const r = await fetch(`/api/note?path=${encodeURIComponent(path)}`);
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || `${r.status}`);
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
            ...defaultKeymap, ...historyKeymap,
          ]),
          markdown(),
          livePreview,
          targetField,
          planDatesField,
          vaultTheme,
          EditorView.lineWrapping,
          placeholder("Empty note."),
          EditorView.updateListener.of((u) => {
            if (u.docChanged) setState((s) => (s === "conflict" ? s : "dirty"));
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
          {statusLine(state, ephemeral)}
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
