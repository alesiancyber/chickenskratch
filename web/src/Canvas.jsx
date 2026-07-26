import { useCallback, useEffect, useRef, useState } from "react";
import { noteHref, postJSON } from "./api.js";
import { linkifyBlock } from "./text.jsx";

/* The canvas: dump anything in. One beautifully typographed input
   (V1 uses NO editor engine — nothing fights us inside a text buffer);
   submitted dumps settle into history blocks below with their results
   attached. //command lines execute instantly (I3 trust), everything
   else rides the capture pipeline. History is scratch — the vault is
   the archive (the dump itself is snapshot-committed on Capture.md). */

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

function QueuedProposal({ p, verdict, error, onDecide }) {
  /* per-task ✓/✗ — partial approval, the prototype-parity amendment.
     Steps map 1:1 to the capture proposal's apply entries. */
  const stepLabels = p.issues.slice(0, p.apply.length);
  const [checked, setChecked] = useState(() => new Set(stepLabels.map((_, i) => i)));
  const partial = checked.size < stepLabels.length;

  const toggle = (i) => setChecked((c) => {
    const n = new Set(c);
    n.has(i) ? n.delete(i) : n.add(i);
    return n;
  });

  if (verdict)
    return (
      <div className="canvas-proposal decided">
        <div className="row" style={{ margin: 0 }}>
          <span className="grow">{p.title}</span>
          <span className={`chip verdict-${verdict}`}>
            {verdict === "approve" ? "approved" : "rejected"}
          </span>
        </div>
      </div>
    );

  return (
    <div className="canvas-proposal">
      <div className="row" style={{ margin: 0 }}>
        <span className="grow">{p.title}</span>
        <span className="decide-buttons" style={{ opacity: 1 }}>
          <button className="decide reject" onClick={() => onDecide(p.id, "reject")}>
            reject all
          </button>
        </span>
      </div>
      <div className="steps">
        {stepLabels.map((label, i) => (
          <button className="row step" key={i} onClick={() => toggle(i)}>
            <span className={"box" + (checked.has(i) ? " on" : "")} />
            <span className="grow" style={{ textAlign: "left" }}>{label}</span>
          </button>
        ))}
      </div>
      <button className="decide approve approve-all"
              disabled={checked.size === 0}
              onClick={() => onDecide(p.id, "approve",
                partial ? [...checked].sort((a, b) => a - b) : undefined)}>
        {partial ? `Approve selected (${checked.size})` : "Approve all"}
      </button>
      {error && <div className="ghost small">{error}</div>}
    </div>
  );
}

function DumpBlock({ entry, acted, errors, onDecide, health }) {
  const auto = entry.summary?.auto || 0;
  const quiet = !entry.error && !entry.deferred && auto === 0
    && entry.queued.length === 0 && entry.commands.length === 0;
  return (
    <div className="dump">
      <div className="dump-meta small ghost">
        {entry.when}
        {auto > 0 && <span className="applied"> · ✓ filed → <a
          href={noteHref(health, "Tasks/Inbox.md")} className="doc-link">Inbox</a></span>}
      </div>
      <div className="dump-text">{entry.text}</div>
      {entry.commands.map((c, i) => <Callout key={i} {...c} />)}
      {entry.queued.map((p) => (
        <QueuedProposal key={p.id} p={p} verdict={acted[p.id]}
                        error={errors[p.id]} onDecide={onDecide} />
      ))}
      {entry.error && <div className="ghost small">{entry.error}</div>}
      {/* The engine was mid-pass, so extraction hasn't run yet. The dump
          itself is already committed to Capture.md, so this is a delay,
          not a loss — saying so beats an empty "nothing extractable". */}
      {entry.deferred && (
        <div className="ghost small">
          saved to Capture.md — the engine was busy; it extracts on its next pass
        </div>
      )}
      {quiet && <div className="ghost small">nothing extractable — kept on Capture.md</div>}
    </div>
  );
}

export default function Canvas({ health, seed, onSeedConsumed }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState([]);
  const [acted, setActed] = useState({});
  const [errors, setErrors] = useState({});   // pid -> why the decide failed
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
      setHistory((h) => [{ id: Date.now(), text: t, when: stamp(),
        commands: res.commands ?? [], queued: res.queued ?? [],
        deferred: Boolean(res.deferred),
        summary: res.summary ?? {} }, ...h]);
      setText("");
      if (taRef.current) taRef.current.style.height = "auto";
    } catch (e) {
      setHistory((h) => [{ id: Date.now(), text: t, when: stamp(),
        commands: [], queued: [], summary: {}, error: String(e.message || e) }, ...h]);
    }
    setBusy(false);
    taRef.current?.focus();
  }, [text, busy]);

  const decide = useCallback(async (pid, verdict, steps) => {
    setActed((a) => ({ ...a, [pid]: verdict }));
    setErrors((e) => { const { [pid]: _gone, ...rest } = e; return rest; });
    try {
      await postJSON("/api/decide", { pid, verdict, ...(steps ? { steps } : {}) });
    } catch (e) {
      // Restore the buttons AND say why — a 409 (the engine was mid-pass)
      // means nothing was applied, and a silently reverted click reads as
      // the UI ignoring you.
      setActed((a) => { const { [pid]: _gone, ...rest } = a; return rest; });
      setErrors((es) => ({ ...es, [pid]: String(e.message || e) }));
    }
  }, []);

  /* Compose stays put; only the history scrolls. The input is the point
     of this surface — burying it under a long result would mean scrolling
     back up to dump the next thing. */
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
            // where it would swallow the history underneath
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
        {history.map((entry) => (
          <DumpBlock key={entry.id} entry={entry} acted={acted} errors={errors}
                     onDecide={decide} health={health} />
        ))}
      </div>
    </div>
  );
}
