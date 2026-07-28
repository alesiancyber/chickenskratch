import { useCallback, useEffect, useRef, useState } from "react";

/* One reader for every JSON GET. Status is checked BEFORE parsing, and a
   body that isn't JSON gets said so plainly — a bare JSON.parse failure
   surfaces as "The string did not match the expected pattern", which
   names neither the route nor the cause. */
export async function getJSON(path) {
  const r = await fetch(path);
  if (!r.ok) {
    // The server's own message when it sent one — it knows why better
    // than we do (stale note hash, vault locked, bad path).
    const said = await r.json().then((d) => d.error).catch(() => "");
    if (said) throw new Error(said);
    // A 404 on /api/* is nearly always a page newer than the server it is
    // talking to: StaticFiles answers whatever the routes don't, so a
    // route added since the process started returns plain-text Not Found.
    const hint = r.status === 404 && path.startsWith("/api/")
      ? " — no such route; restart tezcat-ui to pick up a newer build"
      : "";
    throw new Error(`${path}: ${r.status}${hint}`);
  }
  try {
    return await r.json();
  } catch {
    throw new Error(`${path}: server did not return JSON`);
  }
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
    today: null, review: null, changes: null, health: null, tree: null, error: null,
  });
  const [live, setLive] = useState(false);
  const [lastPassAt, setLastPassAt] = useState(null); // Date of last pass
  const [activity, setActivity] = useState(IDLE);
  // The last draft-side worker failure (down, rate-limited). Its own
  // signal because a pass that drafted nothing looks identical to a quiet
  // vault — and a dump that never got extracted must not read as one.
  const [workerError, setWorkerError] = useState(null);
  const alive = useRef(true);
  /* Refetches overlap: pass_complete and vault_changed can land together,
     and each fires four parallel requests. Without a sequence number the
     SLOWER (older) response wins and paints stale data over fresh. Only
     the newest refetch may write state. */
  const seq = useRef(0);

  /* `what` narrows the refetch. A checked task changes the task lists and
     nothing else — re-pulling git log and worker health for it is the
     difference between instant and a beat behind. */
  const refetch = useCallback(
    async (what = ["today", "review", "changes", "health", "tree"]) => {
    const mine = ++seq.current;
    try {
      const want = new Set(what);
      const [today, review, changes, health, tree] = await Promise.all([
        want.has("today") ? getJSON("/api/today") : null,
        want.has("review") ? getJSON("/api/review") : null,
        want.has("changes") ? getJSON("/api/changes") : null,
        want.has("health") ? getJSON("/api/health") : null,
        want.has("tree") ? getJSON("/api/tree") : null,
      ]);
      if (!alive.current || mine !== seq.current) return;
      setState((s) => ({
        ...s,
        today: today ?? s.today,
        review: review ?? s.review,
        changes: changes ?? s.changes,
        health: health ?? s.health,
        tree: tree ?? s.tree,
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
    es.addEventListener("worker_error", (e) => {
      try { setWorkerError(JSON.parse(e.data)); } catch { /* malformed: ignore */ }
    });
    es.addEventListener("pass_complete", () => {
      setActivity(IDLE);
      setLastPassAt(new Date());
      refetch();                       // a pass can touch everything
    });
    es.addEventListener("vault_changed", (e) => {
      // one edit: the lists move, the git log and worker health don't.
      // The tree moves whenever a note can appear or vanish — which is
      // everything except a checked task, the one change that provably
      // only rewrites a line inside a note that already existed.
      const reason = (() => { try { return JSON.parse(e.data).reason; } catch { return ""; } })();
      if (reason === "task_done" || reason === "reschedule") refetch(["today", "review"]);
      else refetch(reason === "revert"
        ? ["today", "review", "changes", "tree"]
        : ["today", "review", "tree"]);
    });
    return () => { alive.current = false; es.close(); };
  }, [refetch]);

  return { ...state, live, lastPassAt, activity, workerError,
           clearWorkerError: () => setWorkerError(null), refetch };
}

/* Note references open OUR editor. There is deliberately no
   deep-link-out helper: tezcat is the surface, not a launcher for one.
   The vault stays plain markdown any editor can open — that's
   portability, a property of the format, not a handoff we owe another
   app from inside our own UI. */

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
