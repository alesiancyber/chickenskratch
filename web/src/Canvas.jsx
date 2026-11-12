import { useCallback, useEffect, useRef, useState } from "react";
import { postJSON } from "./api.js";
import { listKey } from "./indent.js";
import { findFences } from "./mermaid.js";
import { Diagram, linkifyBlock } from "./text.jsx";

/* The canvas: dump anything in. One beautifully typographed input
   (V1 uses NO editor engine — nothing fights us inside a text buffer).
   The canvas keeps NO history of its own — one dump, one receipt, and
   the results live on Today: auto-applies under "What changed without
   you", queued proposals under "What needs a decision?". The dashboard
   is the one history there is (plus git, the archive) — a second
   scrollback here was a second place to look for the same events.
   //command lines still execute instantly (I3 trust) and their results
   render here, because an answer belongs next to the question. */

function stamp() {
  const d = new Date();
  return `Today ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function Callout({ command, args, result, onOpenNote }) {
  return (
    <div className="callout">
      <div className="callout-title small">//{command} {args}</div>
      {result == null
        ? <div className="ghost small">no result — the server didn’t answer; try again</div>
        : <div className="callout-body small">{linkifyBlock(result, onOpenNote)}</div>}
    </div>
  );
}

/* The latest dump's receipt: where each result went, not the result
   itself. The rows it points at are live on the dashboard column
   beside this one. */
function Receipt({ last, onOpenNote }) {
  const stalled = last.workerErrors.length > 0;
  const quiet = !last.error && !last.deferred && !stalled && last.applied === 0
    && last.queued === 0 && last.commands.length === 0;
  return (
    <div className="dump">
      <div className="dump-meta small ghost">{last.when}</div>
      {last.commands.map((c, i) => <Callout key={i} {...c} onOpenNote={onOpenNote} />)}
      {last.applied > 0 && (
        <div className="small">
          <span className="applied">✓ {last.applied} filed</span>
          <span className="ghost"> — receipts under “What changed without you”</span>
        </div>
      )}
      {last.queued > 0 && (
        <div className="small meta">
          {last.queued} proposal{last.queued > 1 ? "s" : ""} queued — decide
          under “What needs a decision?”
        </div>
      )}
      {last.error && <div className="ghost small">{last.error}</div>}
      {/* The worker never answered (down, rate-limited, misconfigured).
          The dump is already committed to Capture.md and re-arms, so this
          is a delay with a cause — never "nothing extractable", which
          would tell you your text was empty when it wasn't. */}
      {stalled && (
        <div className="small">
          <span className="attention">extraction didn’t run</span>
          {/* Only while it's true: a plan that failed its schema often
              fails the same way every time, so the engine retries it a
              few times and then parks it. Saying "retries on its next
              pass" about a parked item is the same lie as "nothing
              extractable" — it just takes longer to notice. */}
          <span className="ghost"> — saved to Capture.md; {last.workerErrors.some((w) => w.retrying)
            ? "the engine retries on its next pass"
            : "edit the text to try again"}</span>
          {last.workerErrors.map((w, i) => (
            <div className="ghost small" key={i}>{w.error}</div>
          ))}
        </div>
      )}
      {/* The engine was mid-pass, so extraction hasn't run yet. The dump
          itself is already committed to Capture.md, so this is a delay,
          not a loss — saying so beats an empty "nothing extractable". */}
      {last.deferred && (
        <div className="ghost small">
          saved to Capture.md — the engine was busy; it extracts on its next pass
        </div>
      )}
      {/* Every dump drafts a record proposal now, so a quiet pass means
          something narrower: the engine had already processed this exact
          text (the seen-hash) and won't re-ask. The words are still on
          the surface either way. */}
      {quiet && (
        <div className="ghost small">
          Nothing new to file — your text is still on Capture.md.
        </div>
      )}
    </div>
  );
}


/* The dump's diagrams, drawn while you type. The input stays a plain
   textarea (nothing fights the buffer), so a diagram is shown BESIDE
   the text rather than in place of it, and what counts as a diagram is
   the editor's rule — findFences: a closed ```mermaid block. An
   unclosed one is still being typed and draws nothing. Debounced,
   because mermaid re-parses the whole block per change and a diagram
   redrawn on every keystroke flickers through its own syntax errors.
   Preview only: the dump is filed exactly as typed. */
const PREVIEW_DEBOUNCE_MS = 400;
function usePreviewDiagrams(text) {
  const [codes, setCodes] = useState([]);
  useEffect(() => {
    const t = setTimeout(() => {
      const lines = text.split("\n");
      setCodes(text.includes("```")
        ? findFences(lines)
            .map(({ start, end }) => lines.slice(start + 1, end).join("\n"))
            .filter((code) => code.trim())
        : []);
    }, PREVIEW_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [text]);
  return codes;
}

export default function Canvas({ seed, onSeedConsumed, onIngested, onOpenNote }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [last, setLast] = useState(null);
  const taRef = useRef(null);
  const preview = usePreviewDiagrams(text);

  useEffect(() => {
    if (seed) {
      setText(seed);
      onSeedConsumed?.();
      setTimeout(() => {
        const el = taRef.current;
        if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
      }, 60);
    }
  }, [seed, onSeedConsumed]);

  const submit = useCallback(async () => {
    const t = text.trim();
    if (!t || busy) return;
    setBusy(true);
    try {
      const res = await postJSON("/api/ingest", { text: t });
      setLast({ when: stamp(), commands: res.commands ?? [],
        applied: (res.applied ?? []).length,
        queued: (res.queued ?? []).length,
        workerErrors: res.worker_errors ?? [],
        deferred: Boolean(res.deferred), error: null });
      // the receipts render on Today, so freshen it now
      onIngested?.();
      setText("");
      if (taRef.current) taRef.current.style.height = "auto";
    } catch (e) {
      setLast({ when: stamp(), commands: [], applied: 0, queued: 0,
        workerErrors: [], deferred: false, error: String(e.message || e) });
    }
    setBusy(false);
    taRef.current?.focus();
  }, [text, busy, onIngested]);

  /* Compose stays put; only the receipt area scrolls (a long //command
     answer can still run). The input is the point of this surface —
     so a click anywhere on it that isn't a link, a button, or a text
     selection lands focus in the input. Typing at a surface that LOOKS
     focused but isn't is how "//ask" ends up firing review shortcuts. */
  const refocus = (e) => {
    if (e.target.closest("a, button, textarea")) return;
    if (window.getSelection?.()?.toString()) return;  // copying a receipt
    taRef.current?.focus();
  };
  return (
    <div className="canvas" onClick={refocus}>
      <div className="canvas-compose">
        <textarea
          ref={taRef}
          className="canvas-input"
          placeholder="Dump anything."
          value={text}
          autoFocus
          onChange={(e) => setText(e.target.value)}
          onInput={(e) => {
            e.target.style.height = "auto";
            // capped: a long dump grows the box, but never past the point
            // where it would swallow the receipt underneath. Half the
            // viewport, not a fixed 260px — the canvas is the page's
            // main surface now and should spend the room it has.
            e.target.style.height = Math.min(
              e.target.scrollHeight, Math.round(window.innerHeight * 0.5)) + "px";
          }}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
              e.preventDefault();
              submit();
              return;
            }
            /* List keys: Tab indents (a subtask is an indented task —
               a plain textarea would move focus instead), Shift-Tab
               outdents, Enter continues the list. Escape then Tab
               still leaves the box for keyboard users. */
            if (e.key === "Escape") {
              e.currentTarget.blur();
              return;
            }
            if (listKey(e.currentTarget, e)) e.preventDefault();
          }}
        />
        {preview.length > 0 && (
          <div className="canvas-preview">
            {preview.map((code, i) => <Diagram key={i} code={code} />)}
          </div>
        )}
        <div className="ghost small canvas-hint">
          {busy ? "…" : "⌘↩ to submit · Tab indents a subtask · //ask and //azure run instantly"}
        </div>
      </div>
      <div className="dump-history">
        {last && <Receipt last={last} onOpenNote={onOpenNote} />}
      </div>
    </div>
  );
}
