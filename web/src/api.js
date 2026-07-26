import { useCallback, useEffect, useRef, useState } from "react";

async function getJSON(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

/* Write verbs (decide/revert/pass/cancel). application/json is
   load-bearing: the server's consent channel rejects anything else. */
export async function postJSON(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `${path}: ${r.status}`);
  return data;
}

/* The stop button's channel. Cooperative on the server: this returns
   immediately and the job unwinds at its next safe checkpoint, so the
   caller should show "stopping…" until the job's own completion event
   lands rather than assuming it died. */
export const cancelJob = (what) => postJSON("/api/cancel", { what });

const IDLE = { running: false, label: "", stopping: false };

/* One hook owns all server state. REST paints the initial screen; the
   SSE stream (the engine's pass_started / draft_started /
   pass_complete / vault_changed events) marks it stale and triggers a
   refetch — no polling, no spinners: stale data stays on screen until
   fresh data replaces it. `activity` is the same stream read as a
   liveness signal: what the small model is chewing on right now. */
export function useDashboard() {
  const [state, setState] = useState({
    today: null, review: null, changes: null, health: null, error: null,
  });
  const [live, setLive] = useState(false);
  const [lastPassAt, setLastPassAt] = useState(null); // Date of last pass
  const [activity, setActivity] = useState(IDLE);
  const alive = useRef(true);
  /* Refetches overlap: pass_complete and vault_changed can land together,
     and each fires four parallel requests. Without a sequence number the
     SLOWER (older) response wins and paints stale data over fresh. Only
     the newest refetch may write state. */
  const seq = useRef(0);

  /* `what` narrows the refetch. A checked task changes the task lists and
     nothing else — re-pulling git log and worker health for it is the
     difference between instant and a beat behind. */
  const refetch = useCallback(async (what = ["today", "review", "changes", "health"]) => {
    const mine = ++seq.current;
    try {
      const want = new Set(what);
      const [today, review, changes, health] = await Promise.all([
        want.has("today") ? getJSON("/api/today") : null,
        want.has("review") ? getJSON("/api/review") : null,
        want.has("changes") ? getJSON("/api/changes") : null,
        want.has("health") ? getJSON("/api/health") : null,
      ]);
      if (!alive.current || mine !== seq.current) return;
      setState((s) => ({
        ...s,
        today: today ?? s.today,
        review: review ?? s.review,
        changes: changes ?? s.changes,
        health: health ?? s.health,
        error: null,
      }));
      if (health?.last_pass)
        setLastPassAt((prev) => {
          const t = new Date(health.last_pass);
          return prev && prev > t ? prev : t;
        });
    } catch (e) {
      if (alive.current && mine === seq.current)
        setState((s) => ({ ...s, error: String(e) }));
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    refetch();
    const es = new EventSource("/api/events");
    es.onopen = () => setLive(true);
    es.onerror = () => setLive(false);

    es.addEventListener("pass_started", () =>
      setActivity({ running: true, label: "scanning the vault", stopping: false }));
    es.addEventListener("draft_started", (e) => {
      const d = JSON.parse(e.data);
      setActivity((a) => ({
        ...a,
        running: true,
        label: `${d.processor} · ${noteName(d.note || "")}`,
      }));
    });
    es.addEventListener("cancel_requested", (e) => {
      if (JSON.parse(e.data).what === "pass")
        setActivity((a) => (a.running ? { ...a, stopping: true } : a));
    });
    es.addEventListener("pass_complete", () => {
      setActivity(IDLE);
      setLastPassAt(new Date());
      refetch();                       // a pass can touch everything
    });
    es.addEventListener("vault_changed", (e) => {
      // one edit: the lists move, the git log and worker health don't
      const reason = (() => { try { return JSON.parse(e.data).reason; } catch { return ""; } })();
      refetch(reason === "revert"
        ? ["today", "review", "changes"]
        : ["today", "review"]);
    });
    return () => { alive.current = false; es.close(); };
  }, [refetch]);

  return { ...state, live, lastPassAt, activity, refetch };
}

/* Deep links out to the editor — the UI never edits notes (the editor
   already does it better). Scheme comes from Spec.md via /api/health. */
export function noteHref(health, note) {
  if (!health) return "#";
  const abs = `${health.vault}/${note}`;
  return health.link_scheme === "vscode"
    ? `vscode://file/${encodeURIComponent(abs)}`
    : `obsidian://open?path=${encodeURIComponent(abs)}`;
}

export const noteName = (note) => note.replace(/\.md$/, "").split("/").pop();

/* Split-pane fraction, persisted so a chosen layout survives reload.
   Clamped on read: a corrupt or out-of-range value must never render a
   collapsed column with no way to drag it back. */
export function useSplit(key, initial = 0.55, min = 0.2, max = 0.8) {
  const [frac, setFrac] = useState(() => {
    const v = parseFloat(localStorage.getItem(key));
    return Number.isFinite(v) && v >= min && v <= max ? v : initial;
  });
  useEffect(() => { localStorage.setItem(key, String(frac)); }, [key, frac]);
  const set = useCallback(
    (f) => setFrac(Math.min(max, Math.max(min, f))), [min, max]);
  return [frac, set];
}
