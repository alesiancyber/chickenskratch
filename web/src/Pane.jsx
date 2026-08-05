import { useCallback, useEffect, useRef, useState } from "react";
import { cancelJob, postJSON } from "./api.js";
import { linkifyBlock } from "./text.jsx";

/* The escape-hatch pane (Phase 4). NOT a third surface: the frozen
   chat escape hatch re-homed where its tool activity is visible.
   Docked bottom-right so it never costs a column — minimized to its
   own title bar, summoned (⌘J), closed when done. Tool calls render
   as rows expandable to the raw result; write-gate prompts are
   Allow/Deny chips; transcripts are scratch — the vault is the
   archive. If this pane is open all day, that's a processor backlog
   signal, not a reason to grow it. */

function ToolRow({ item }) {
  const [open, setOpen] = useState(false);
  const args = JSON.stringify(item.args);
  return (
    <div className={"toolrow" + (open ? " open" : "")}
         onClick={() => setOpen((v) => !v)} title="Click to expand result">
      <span className="ghost">→ </span>
      {item.name}({args.length > 60 ? args.slice(0, 60) + "…" : args})
      {open && (
        <pre>{item.result ?? "(no result yet)"}</pre>
      )}
    </div>
  );
}

function Gate({ item, onAnswer }) {
  if (item.resolved != null)
    return (
      <div className="gate settled small">
        <span className={`chip verdict-${item.resolved ? "approve" : "reject"}`}>
          {item.resolved ? "allowed" : "denied"}
        </span>{" "}
        <span className="mono-ghost">{item.name}(…)</span>
      </div>
    );
  return (
    <div className="gate">
      <div className="small">Write tool call — <b>needs your approval</b>:</div>
      <div className="g-call">{item.name}({JSON.stringify(item.args)})</div>
      <div className="g-acts">
        <button className="decide approve" onClick={() => onAnswer(item.id, true)}>
          Allow
        </button>
        <button className="decide reject" onClick={() => onAnswer(item.id, false)}>
          Deny
        </button>
      </div>
    </div>
  );
}

export default function Pane({ open, onClose, onOpenNote }) {
  const [items, setItems] = useState([]);
  const [meta, setMeta] = useState(null);   // {model, servers} once ready
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [min, setMin] = useState(false);
  // Bumped when the server swaps sessions under us (stop = kill-and-
  // replace): the old SSE stream belongs to a dead session, so tear it
  // down and subscribe to the fresh one.
  const [gen, setGen] = useState(0);
  // The stream connects on FIRST open, not on mount: the pane is closed
  // by default, and a session that never opens it should not hold a
  // server connection ticking for nobody. Once opened it stays
  // connected across closes, so a turn finishing while the pane is
  // tucked away still lands in the transcript.
  const [started, setStarted] = useState(false);
  useEffect(() => { if (open) setStarted(true); }, [open]);
  const scroller = useRef(null);
  const inputRef = useRef(null);

  /* The session swap, from either of the two things that can announce
     it: our own stop button's 202, and the "reset" event the server
     publishes on the dying stream. Both fire on an ordinary stop, and
     whichever loses the race must not swap a SECOND time — bumping gen
     twice tears down the stream we just opened against the new session
     and opens a third. `fromGen` is the generation the caller observed,
     so the late one is a no-op by construction rather than by timing. */
  const dropSession = useCallback((fromGen) => {
    setGen((g) => (g === fromGen ? g + 1 : g));
    setItems([{ kind: "info", text: "stopped — fresh chat" }]);
    setBusy(false);
    setStopping(false);
  }, []);

  useEffect(() => {
    if (!started) return undefined;
    const es = new EventSource("/api/chat/events");
    const add = (item) => setItems((xs) => [...xs, item]);
    es.addEventListener("ready", (e) => setMeta(JSON.parse(e.data)));
    es.addEventListener("user", (e) =>
      add({ kind: "user", text: JSON.parse(e.data).text }));
    es.addEventListener("assistant", (e) => {
      add({ kind: "assistant", text: JSON.parse(e.data).content });
      setBusy(false);
      setStopping(false);
    });
    // the server acknowledging the stop; "reset" follows immediately
    es.addEventListener("stopping", () => setStopping(true));
    // stop = kill-and-replace: the session this stream watches is dead
    // and a fresh one is already live. Transcripts are scratch, so drop
    // everything, free the input, and reconnect to the new session —
    // the pane must never sit dead on "stopping…".
    es.addEventListener("reset", () => dropSession(gen));
    // "chat_error": server-side chat failures ("error" is reserved by
    // EventSource for connection drops, which it retries itself)
    es.addEventListener("chat_error", (e) => {
      add({ kind: "error", text: JSON.parse(e.data).message });
      setBusy(false);
      setStopping(false);
    });
    es.addEventListener("tool", (e) => {
      const d = JSON.parse(e.data);
      add({ kind: "tool", name: d.name, args: d.args, result: null });
    });
    es.addEventListener("tool_result", (e) => {
      const d = JSON.parse(e.data);
      setItems((xs) => {
        const i = xs.findLastIndex((x) => x.kind === "tool" && x.name === d.name && x.result == null);
        if (i < 0) return xs;
        const next = xs.slice();
        next[i] = { ...next[i], result: d.result };
        return next;
      });
    });
    es.addEventListener("confirm_request", (e) => {
      const d = JSON.parse(e.data);
      add({ kind: "gate", id: d.id, name: d.name, args: d.args, resolved: null });
    });
    es.addEventListener("confirm_resolved", (e) => {
      const d = JSON.parse(e.data);
      setItems((xs) => xs.map((x) =>
        x.kind === "gate" && x.id === d.id ? { ...x, resolved: d.approved } : x));
    });
    return () => es.close();
  }, [gen, started, dropSession]);

  useEffect(() => {
    if (scroller.current) scroller.current.scrollTop = 1e6;
  }, [items]);

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 120);
  }, [open]);

  const send = useCallback(async () => {
    const t = text.trim();
    if (!t || busy) return;
    setText("");
    setBusy(true);
    try {
      await postJSON("/api/chat", { text: t });
    } catch (e) {
      setItems((xs) => [...xs, { kind: "error", text: String(e.message || e) }]);
      setBusy(false);
    }
  }, [text, busy]);

  /* Stop kills the chat and a fresh one opens instantly — the server
     swaps sessions before answering, so the 202 itself means "the new
     session is live": clear the pane right here rather than trusting
     the "reset" event to arrive on a stream that may be mid-reconnect.
     The reset handler stays for other tabs. "stopping…" only flashes. */
  const stop = useCallback(async () => {
    setStopping(true);
    try {
      await cancelJob("chat");
      dropSession(gen);
    } catch {
      setStopping(false);
    }
  }, [dropSession, gen]);

  const answer = useCallback(async (id, allow) => {
    try {
      await postJSON("/api/chat/confirm", { id, allow });
    } catch {
      /* expired chip: the resolve event (deny) already came through */
    }
  }, []);

  return (
    <aside className={"pane" + (open ? " open" : "") + (min ? " min" : "")}
           aria-label="Chat — the escape hatch">
      <div className="pane-head" onDoubleClick={() => setMin((v) => !v)}>
        <span className="pane-title">Chat</span>
        {busy && <span className="pulse" title="working" />}
        <span className="pane-sub small">
          {min ? (busy ? "working…" : "minimized")
               : `escape hatch${meta ? ` · ${meta.model}` : ""} · vault-only tools`}
        </span>
        <button className="pane-close" onClick={() => setMin((v) => !v)}
                aria-label={min ? "Expand pane" : "Minimize pane"}
                title={min ? "Expand" : "Minimize"}>{min ? "▲" : "▁"}</button>
        <button className="pane-close" onClick={onClose} aria-label="Close pane">✕</button>
      </div>
      <div className="msgs" ref={scroller}>
        {items.length === 0 && (
          <div className="ghost small">
            For what no processor or command covers yet. A prompt you issue
            twice is a processor candidate.
          </div>
        )}
        {items.map((item, i) => {
          if (item.kind === "user")
            return <div className="msg u" key={i}>{item.text}</div>;
          if (item.kind === "assistant")
            return <div className="msg" key={i}>{linkifyBlock(item.text, onOpenNote)}</div>;
          if (item.kind === "tool")
            return <ToolRow key={i} item={item} />;
          if (item.kind === "gate")
            return <Gate key={i} item={item} onAnswer={answer} />;
          return <div className="ghost small" key={i}>{item.text}</div>;
        })}
        {busy && (
          <div className="working small">
            <span className="pulse" />
            <span className="ghost">{stopping ? "stopping…" : "thinking"}</span>
            {!stopping && (
              <button className="stop-btn" onClick={stop}
                      title="Kill this chat — a fresh one opens instantly">
                stop
              </button>
            )}
          </div>
        )}
      </div>
      <div className="pane-input">
        <textarea
          ref={inputRef}
          rows={1}
          placeholder="Ask, or ask it to do something…"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            } else if (e.key === "Escape" && busy && !stopping) {
              e.preventDefault();
              stop();  // stop the turn without leaving the input
            }
          }}
        />
        <span className="ghost" style={{ fontSize: "11px" }}>
          ⏎ send · esc stops (fresh chat) · write tools always ask first
        </span>
      </div>
    </aside>
  );
}
