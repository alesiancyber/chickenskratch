import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";
import { EditorState } from "@codemirror/state";
import { EditorView, keymap, placeholder } from "@codemirror/view";
import { useCallback, useEffect, useRef, useState } from "react";
import { postJSON } from "./api.js";
import { livePreview, setTarget, targetField, vaultTheme } from "./livepreview.js";

const TARGET_FADE_MS = 2600;

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
  if (ephemeral) return "saved · re-rendered each pass, so edits here are temporary";
  return "saved";
}

export default function NoteEditor({ path, target, seq, visible = true, onClose }) {
  const host = useRef(null);
  const view = useRef(null);
  const baseHash = useRef("");
  const saveRef = useRef(() => {});
  const fadeTimer = useRef(null);
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
      // 409 from the server: the engine (or you, in Obsidian) got here first
      setState(/changed on disk/.test(msg) ? "conflict" : "error");
      setError(msg);
    }
  }, [path]);
  saveRef.current = save;

  /* Scroll the target into view and mark it briefly. Two kinds arrive:
     a dashboard row passes its task text (a substring of the raw line
     "- [ ] Sam: … ^t-abc", so no re-parsing needed), and a chat citation
     passes the line number it drew from. */
  const reveal = useCallback(() => {
    const v = view.current;
    if (!v || !target) return;
    const doc = v.state.doc;
    let at;
    if (/^\d+$/.test(target)) {
      const n = Number(target);
      if (n < 1 || n > doc.lines) return;
      at = doc.line(n).from;
    } else {
      at = doc.toString().indexOf(target);
      if (at < 0) return;
    }
    const line = doc.lineAt(at);
    v.dispatch({
      effects: [
        setTarget.of(line.from),
        EditorView.scrollIntoView(line.from, { y: "center" }),
      ],
    });
    clearTimeout(fadeTimer.current);
    fadeTimer.current = setTimeout(() => {
      view.current?.dispatch({ effects: setTarget.of(null) });
    }, TARGET_FADE_MS);
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

  /* Re-reveal when the dashboard points at a different row of the SAME
     note (`seq` also changes on a repeat click of the same row, so the
     mark comes back rather than appearing to do nothing). */
  useEffect(() => { if (target) revealRef.current(); }, [target, seq]);

  /* The editor stays mounted while the canvas is showing — switching
     tabs must not discard unsaved edits — but CodeMirror measures itself
     against a laid-out DOM, so it needs a nudge when it comes back. */
  useEffect(() => {
    if (visible) view.current?.requestMeasure();
  }, [visible]);

  useEffect(() => () => clearTimeout(fadeTimer.current), []);

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
