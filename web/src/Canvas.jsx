import { useCallback, useEffect, useRef, useState } from "react";
import { postJSON } from "./api.js";
import { linkifyBlock } from "./text.jsx";

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

function Callout({ command, args, result }) {
  return (
    <div className="callout">
      <div className="callout-title small">//{command} {args}</div>
      {result == null
        ? <div className="ghost small">no result — the docs server didn’t answer; try again</div>
        : <div className="callout-body small">{linkifyBlock(result)}</div>}
    </div>
  );
}

/* The latest dump's receipt: where each result went, not the result
   itself. The rows it points at are live on the dashboard column
   beside this one. */
function Receipt({ last }) {
  const stalled = last.workerErrors.length > 0;
  const quiet = !last.error && !last.deferred && !stalled && last.applied === 0
    && last.queued === 0 && last.commands.length === 0;
  return (
    <div className="dump">
      <div className="dump-meta small ghost">{last.when}</div>
      {last.commands.map((c, i) => <Callout key={i} {...c} />)}
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
          <span className="ghost"> — saved to Capture.md; the engine retries
            on its next pass</span>
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
      {quiet && <div className="ghost small">nothing extractable — kept on Capture.md</div>}
    </div>
  );
}

export default function Canvas({ seed, onSeedConsumed, onIngested }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [last, setLast] = useState(null);
  const taRef = useRef(null);

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
     answer can still run). The input is the point of this surface. */
  return (
    <div className="canvas">
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
            // where it would swallow the receipt underneath
            e.target.style.height = Math.min(e.target.scrollHeight, 260) + "px";
          }}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
              e.preventDefault();
              submit();
            }
          }}
        />
        <div className="ghost small canvas-hint">
          {busy ? "…" : "⌘↩ to submit · //azure … runs instantly"}
        </div>
      </div>
      <div className="dump-history">
        {last && <Receipt last={last} />}
      </div>
    </div>
  );
}
