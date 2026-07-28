"""The owned dashboard's server.

Reads (I5, free): JSON view-models — today.collect(), the pending queue
with structured diff payloads (diffs.diff_rows), recent_commits, worker
health — plus one SSE stream tailing the engine's events file. The UI
parses no markdown and owns no state: a wrong read costs a glance, and
a dead UI process degrades to the editor path.

Writes: decide / revert / pass — all human-initiated button verbs, all
through service.py, resolving in the same engine pass and pass lock as
every other consent channel (I1/I2). The channel is hardened as a
consent channel: bound to 127.0.0.1 unconditionally (the localhost
bind is the trust boundary, not a flag), and writes require
application/json from a local origin — a hostile web page's form POST
or cross-origin fetch dies on the content-type preflight or the origin
check. Every route — reads included — also requires a local Host
header: DNS rebinding resolves an attacker's domain to 127.0.0.1 and
reads the vault through the victim's browser as same-origin GETs, and
the one thing a rebound request cannot fake is its Host. Models have
no route here: no MCP server exposes HTTP POST.

The daemon stays the only trigger authority — POST /api/pass asks for
the same pass it would run, nothing more.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from urllib.parse import urlparse

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from tezcat_vault.vault import Vault, VaultError, VaultLocked
from tezcat_vault.vaultconfig import get_config

from . import (
    applier,
    cancel,
    changes,
    commands,
    diffs,
    engine,
    events,
    pane,
    processors,
    proposals,
    service,
    today,
)
from .bootstrap import ROOT
from .watcher import LockBusy

WEB_DIST = ROOT / "web" / "dist"
SSE_POLL_SECONDS = 1.0
SSE_HEARTBEAT_TICKS = 15

# kinds whose issues list IS the payload (ProcessorSpec.render_diff=False)
_NO_DIFF_KINDS = {p.name for p in processors.REGISTRY if not p.render_diff}


def _vault(vault_path: str) -> Vault:
    return Vault(vault_path, exclude=get_config(vault_path).exclude)


def _sse_response(request, pump, cleanup=None) -> StreamingResponse:
    """One SSE stream shape for both event sources. `pump` is an async
    callable returning this tick's (event_name, payload) pairs and owning
    its own pacing — a queue wait or a poll sleep, roughly one second
    either way. Everything else is boilerplate that was written twice:
    the reconnect hint, the disconnect check, and the periodic comment
    ping that stops browsers and proxies reaping an idle stream.

    `cleanup` runs when the stream ends however it ends — browsers drop
    and re-open SSE connections routinely, so a listener that isn't
    released on disconnect leaks one per reconnect."""
    async def stream():
        try:
            yield "retry: 2000\n\n"
            ticks = 0
            while not await request.is_disconnected():
                for name, payload in await pump():
                    yield f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                ticks += 1
                if ticks % SSE_HEARTBEAT_TICKS == 0:
                    yield ": ping\n\n"
        finally:
            if cleanup is not None:
                cleanup()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


RECENT_AUTO_HOURS = 48


def _recent_auto(vault_path: str, cap: int = 8) -> list[dict]:
    """Autonomous applies from the last two days, newest first — the
    audit rows for `autonomy: full`. Each carries the undo shas its
    apply recorded, so the dashboard reverts the DECISION, not a commit
    the user has to go hunting for; `unvouched` marks the ones whose
    verifier objected (the first place to look when something's off)."""
    import datetime as dt

    cutoff = (dt.datetime.now() - dt.timedelta(hours=RECENT_AUTO_HOURS)
              ).isoformat(timespec="seconds")
    rows = []
    for p in proposals.load_all(proposals.dir_for(vault_path)):
        if p.get("status") != "auto":
            continue
        ts = (p.get("resolved") or {}).get("ts", "")
        if ts < cutoff:
            continue
        rows.append({
            "id": p["id"], "kind": p["kind"], "title": p["title"], "ts": ts,
            "note": p["drafted_against"]["note"],
            "summary": p.get("summary", ""),
            "undo": p.get("undo", []),
            "unvouched": bool(p.get("verifier_failures")),
        })
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows[:cap]


def today_model(vault_path: str) -> dict:
    pending = len(proposals.pending(proposals.dir_for(vault_path)))
    model = today.collect(_vault(vault_path), get_config(vault_path), pending)
    model["recent_auto"] = _recent_auto(vault_path)
    return model


def review_model(vault_path: str) -> list[dict]:
    """The pending queue with structured diffs — the same simulate the
    verifiers walk, shipped as JSON rows."""
    v = _vault(vault_path)
    applier.use_vault(vault_path)
    out = []
    for p in sorted(proposals.pending(proposals.dir_for(vault_path)),
                    key=lambda r: r["created"]):
        entry = {
            "id": p["id"], "kind": p["kind"], "title": p["title"],
            "summary": p["summary"], "issues": p["issues"],
            "created": p["created"], "note": p["drafted_against"]["note"],
            "needs_review": bool(p.get("needs_review")),
            "diff": None, "not_previewed": [], "diff_error": "",
            # partial approval: steps map 1:1 to apply entries for the
            # kinds whose plan is a per-item list (captured tasks, plan
            # picks, overlap pairs) — approve some, reject the rest.
            # Overlap steps are replace_line ops, so their label is the
            # matching issue line (same index by construction).
            "steps": [
                {"label": (p["issues"][i] if p["kind"] == "overlap" and i < len(p["issues"])
                           else s["args"].get("text") or s["args"].get("task_text") or s["tool"]),
                 "due": s["args"].get("due", ""),
                 "to": s["args"].get("path", "")}
                for i, s in enumerate(p["apply"])
            ] if p["kind"] in ("capture", "plan_today", "overlap") else [],
        }
        if p["kind"] not in _NO_DIFF_KINDS:
            try:
                note_text = v.read(entry["note"])
            except VaultError:
                entry["diff_error"] = "target note unreadable — proposal will go stale"
            else:
                if len(note_text.splitlines()) > diffs.MAX_PREVIEW_LINES:
                    entry["diff_error"] = "note too large to preview — judge from the summary"
                else:
                    after, skipped = applier.simulate_for_note(note_text, p, entry["note"])
                    entry["diff"] = diffs.diff_rows(note_text, after)
                    entry["not_previewed"] = skipped
        out.append(entry)
    return out


# A vault is a folder of markdown, and the explorer renders the folder.
# Bounded anyway (I6): a pathological vault must cost one big response,
# not an unbounded one, and 5k rows is already past what anyone browses.
MAX_TREE_NOTES = 5000


def tree_model(vault_path: str) -> dict:
    """The file explorer's view-model: every note the harness can see, as
    flat vault-relative paths. Flat on purpose — nesting is a rendering
    decision, and the client already has to sort and fold anyway.

    Exclusions and dot-directories are gone before we ever see a path
    (Vault.iter_notes, one choke point), so Private/ is as invisible here
    as everywhere else (I4). `surface_dirs` rides along so the client can
    ghost the machine-rendered folders instead of hard-coding "Harness"."""
    v = _vault(vault_path)
    notes, truncated = [], False
    for p in v.iter_notes():
        if len(notes) >= MAX_TREE_NOTES:
            truncated = True
            break
        notes.append(v.rel(p))
    return {
        "notes": notes,
        "surface_dirs": sorted(get_config(vault_path).surface_dirs),
        "truncated": truncated,
    }


def changes_model(vault_path: str, limit: int = 20) -> dict:
    """Recent snapshots minus machine renders — the same filter `tezcat
    changes` prints through (changes.recent_news), so the two renderings
    of "what changed" can never disagree."""
    return {"commits": changes.recent_news(
        _vault(vault_path), changes.machine_rels(get_config(vault_path)), limit)}


# Worker reachability is a network probe — ~90ms against a cloud endpoint,
# four times the cost of every other read combined. Paid per refetch, it
# makes a checked task wait on an HTTPS round trip just to say so; whether
# the worker is up doesn't change per keystroke, so it's TTL-cached.
HEALTH_TTL_SECONDS = 15.0
_probe: dict = {"at": 0.0, "base": "", "up": False}


def _worker_reachable(base: str) -> bool:
    from .backends import endpoint_alive_sync

    now = time.monotonic()
    if base != _probe["base"] or now - _probe["at"] > HEALTH_TTL_SECONDS:
        _probe.update(at=now, base=base, up=endpoint_alive_sync(base))
    return bool(_probe["up"])


def health_model(vault_path: str) -> dict:
    from tezcat_vault.workers import worker_base_url, worker_model

    base = worker_base_url()
    head, _dirty = applier.vault_state(vault_path)
    _off, recs = events.read_new(vault_path, 0)
    last_pass = next((r["ts"] for r in reversed(recs)
                      if r.get("event") == "pass_complete"), "")
    return {
        "vault": vault_path,
        "head": head,
        "pending": len(proposals.pending(proposals.dir_for(vault_path))),
        "last_pass": last_pass,
        "worker": {"model": worker_model(), "base_url": base,
                   "reachable": _worker_reachable(base)},
    }


def _snapshots_since(vault_path: str, before_sha: str) -> list[str]:
    """Non-machine snapshot shas newer than `before_sha`, newest first —
    what a UI undo must revert to unwind the action that just ran.

    Its own walk rather than recent_news: the boundary is a SHA, and that
    sha may itself be a machine render — so the stop test has to run
    before the filter, or the walk would sail past the boundary."""
    machine = changes.machine_rels(get_config(vault_path))
    out = []
    for c in _vault(vault_path).history(30):
        if c["sha"] == before_sha:
            break
        if changes.is_machine_render(c, machine):
            continue
        out.append(c["sha"])
    return out


def build_app(vault_path: str, chat_agent_factory=None) -> Starlette:
    def api(model):
        def handler(request):  # sync: Starlette runs it in the threadpool
            return JSONResponse(model(vault_path))
        return handler

    async def guard_write(request):
        """The consent-channel gate. JSON-only forces a CORS preflight
        (form posts are 'simple requests' and would sail through);
        non-local origins are refused outright. Returns (body, error)."""
        if not request.headers.get("content-type", "").startswith("application/json"):
            return None, PlainTextResponse("writes require application/json", status_code=415)
        origin = request.headers.get("origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
            return None, PlainTextResponse("non-local origin refused", status_code=403)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return None, PlainTextResponse("invalid JSON", status_code=400)
        return (body if isinstance(body, dict) else {}), None

    async def decide_route(request):
        body, err = await guard_write(request)
        if err:
            return err
        head_before, _ = await run_in_threadpool(applier.vault_state, vault_path)
        steps = body.get("steps")
        try:
            # On the engine thread: a decision applies a plan synchronously
            # (git subprocesses, file writes). Run on this loop it freezes
            # the whole server — SSE included — for the duration.
            summary = await engine.run(service.decide(
                vault_path, str(body.get("pid", "")), str(body.get("verdict", "")),
                steps=steps if isinstance(steps, list) else None))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except LockBusy as e:
            # 409, not a summary of nothing: the verdict was NOT applied,
            # and the dashboard must drop its optimistic mark and say so.
            return JSONResponse({"error": str(e)}, status_code=409)
        undo = await run_in_threadpool(_snapshots_since, vault_path, head_before)
        return JSONResponse({"summary": summary, "undo": undo})

    async def revert_route(request):
        body, err = await guard_write(request)
        if err:
            return err
        try:
            subject = await run_in_threadpool(
                service.revert, vault_path, str(body.get("sha", "")))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse({"reverted": subject})

    async def ingest_route(request):
        """The canvas submit. Command lines execute instantly (I3 trust:
        the user typed the order); everything else rides the capture
        pipeline through service.ingest — the same extraction, verifier,
        and auto-apply/queue fork the Capture.md scan feeds. One dump,
        two classes of change, exactly like the vault (principle 3)."""
        body, err = await guard_write(request)
        if err:
            return err
        cmd_results, rest = [], []
        for line in str(body.get("text", "")).splitlines():
            parsed = commands.parse(line)
            if parsed:
                name, args = parsed
                try:
                    result = await service.run_command(vault_path, name, args)
                except ValueError as e:
                    result = str(e)
                cmd_results.append({"command": name, "args": args, "result": result})
            else:
                rest.append(line)
        out = {"commands": cmd_results, "summary": {}, "queued": [],
               "applied": [], "deferred": False, "worker_errors": []}
        if "\n".join(rest).strip():
            # Engine thread: ingest runs a FULL pass (scan + draft + render).
            out.update(await engine.run(service.ingest(vault_path, "\n".join(rest))))
        return JSONResponse(out)

    async def note_get_route(request):
        """Read a note into the owned editor. A read, so no consent gate —
        but still 404s through VaultError for anything excluded or absent,
        so Private/ is as invisible here as everywhere else."""
        rel = request.query_params.get("path", "")
        if not rel:
            return JSONResponse({"error": "path is required"}, status_code=400)
        try:
            return JSONResponse(await run_in_threadpool(service.read_note, vault_path, rel))
        except VaultError as e:
            return JSONResponse({"error": str(e)}, status_code=404)

    async def note_put_route(request):
        body, err = await guard_write(request)
        if err:
            return err
        try:
            result = await run_in_threadpool(
                service.write_note, vault_path, str(body.get("path", "")),
                str(body.get("text", "")), str(body.get("hash", "")))
        except VaultLocked as e:           # a wedged writer, not a bad path
            return JSONResponse({"error": str(e)}, status_code=503)
        except VaultError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except RuntimeError as e:          # the note moved under the editor
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse(result)

    async def task_done_route(request):
        body, err = await guard_write(request)
        if err:
            return err
        try:
            result = await run_in_threadpool(
                service.complete_task, vault_path,
                str(body.get("note", "")), str(body.get("text", "")))
        except VaultLocked as e:
            return JSONResponse({"error": str(e)}, status_code=503)
        except VaultError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse(result)

    async def task_due_route(request):
        """The hard-commitment reschedule (📅). Same contract as
        task_done: human-initiated, gated tool, one revertible snapshot
        — plus a corpus row per move."""
        body, err = await guard_write(request)
        if err:
            return err
        try:
            result = await run_in_threadpool(
                service.reschedule_task, vault_path,
                str(body.get("note", "")), str(body.get("text", "")),
                str(body.get("due", "")))
        except VaultLocked as e:
            return JSONResponse({"error": str(e)}, status_code=503)
        except VaultError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse(result)

    async def task_plan_route(request):
        """The judgment verb (⏳): promote into today, defer out. The
        UI's promote/defer buttons land here — never on 📅, so a plan
        can never fabricate a deadline."""
        body, err = await guard_write(request)
        if err:
            return err
        try:
            result = await run_in_threadpool(
                service.plan_task, vault_path,
                str(body.get("note", "")), str(body.get("text", "")),
                str(body.get("when", "")))
        except VaultLocked as e:
            return JSONResponse({"error": str(e)}, status_code=503)
        except VaultError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse(result)

    chat_tasks: set[asyncio.Task] = set()

    async def pass_route(request):
        _body, err = await guard_write(request)
        if err:
            return err
        # Fire and forget ON THE ENGINE THREAD: a full pass can draft for
        # minutes and blocks hard between its awaits; the SSE
        # pass_complete event is the answer. The lock makes an already-
        # running pass a cheap no-op (lock_wait 0 — this button asks for
        # "a pass now", and a running one already is that).
        engine.spawn(service.run_pass(vault_path))
        return JSONResponse({"started": True}, status_code=202)

    # ---- the escape-hatch pane (Phase 4): one session, scratch ----

    chat = pane.ChatSession(vault_path, agent_factory=chat_agent_factory)

    async def cancel_route(request):
        """The stop button. A write verb, so it takes the same
        consent-channel gate as the rest. Two jobs, two shapes:

        - pass: cooperative (cancel.py) — marked, returns immediately,
          unwinds at its next safe checkpoint; the UI shows "stopping…"
          until the pass's own completion event lands.
        - chat: kill-and-replace — a fresh session takes over NOW, and
          the old one is stopped and torn down in the background.
          Waiting out the old turn's unwind is what used to wedge the
          pane on "stopping…" with no way to type; "reset" on the old
          stream tells clients to drop the transcript and reconnect
          (transcripts are scratch — the vault is the archive)."""
        nonlocal chat
        body, err = await guard_write(request)
        if err:
            return err
        what = str(body.get("what", ""))
        if what not in cancel.JOBS:
            return JSONResponse(
                {"error": f"what must be one of {list(cancel.JOBS)}"}, status_code=400)
        if what == cancel.CHAT:
            old, chat = chat, pane.ChatSession(vault_path, agent_factory=chat_agent_factory)
            old.stop()  # marks it dead; denies any gate the turn is parked on
            old.events.publish({"type": "reset"})
            t = asyncio.create_task(old.close())  # teardown off the request path
            chat_tasks.add(t)
            t.add_done_callback(chat_tasks.discard)
        else:
            cancel.request(what)
        events.emit(vault_path, "cancel_requested", {"what": what})
        return JSONResponse({"cancelling": what}, status_code=202)

    async def chat_send_route(request):
        body, err = await guard_write(request)
        if err:
            return err
        text = str(body.get("text", "")).strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)
        # Stays on THIS loop: a chat turn is await-bound (model calls, MCP
        # round trips), and its MCP transports are anyio cancel scopes
        # bound to the task that entered them — the session's own worker
        # task already owns that. Nothing here blocks the loop.
        task = asyncio.create_task(chat.send(text))
        chat_tasks.add(task)
        task.add_done_callback(chat_tasks.discard)
        return JSONResponse({"accepted": True}, status_code=202)

    async def chat_confirm_route(request):
        body, err = await guard_write(request)
        if err:
            return err
        ok = chat.respond(str(body.get("id", "")), bool(body.get("allow")))
        return JSONResponse({"ok": ok}, status_code=200 if ok else 410)

    async def chat_events(request):
        # Its OWN feed, like /api/events. One shared queue would be wrong:
        # get() removes the record, so a second tab — or the overlap while
        # a browser reconnects this very stream — wouldn't get a copy, it
        # would take the other's half of the turn (see fanout.py).
        queue = chat.events.subscribe()

        async def pump():
            try:
                ev = await asyncio.wait_for(queue.get(), SSE_POLL_SECONDS)
            except TimeoutError:
                return []  # idle tick; the wait paced it
            # "error" is EventSource's reserved connection-error name;
            # chat failures ride as chat_error
            return [("chat_error" if ev["type"] == "error" else ev["type"], ev)]

        return _sse_response(request, pump, cleanup=lambda: chat.events.unsubscribe(queue))

    async def sse(request):
        """Live first, tail second. Events raised in this process — a
        click's pass, a checked task, a saved note — arrive the instant
        they're emitted. The file tail still runs on the idle tick so
        events from OTHER processes (tezcat-watch, the cron heartbeat)
        are never missed; it's the fallback, not the mechanism."""
        try:
            offset = events.events_path(vault_path).stat().st_size
        except OSError:
            offset = 0
        queue = events.subscribe()

        async def pump():
            nonlocal offset
            try:
                rec = await asyncio.wait_for(queue.get(), SSE_POLL_SECONDS)
            except TimeoutError:
                offset, recs = events.read_new(vault_path, offset)
                return [(r["event"], r) for r in recs]
            span = rec.pop("_span", None)
            # Only skip past our own record when it sits exactly where the
            # tail is looking; if another process appended in between, we
            # leave the offset alone and let the tail deliver in order (a
            # duplicate refetch is harmless, a dropped event is not).
            if span and span[0] == offset:
                offset = span[1]
            return [(rec["event"], rec)]

        return _sse_response(request, pump, cleanup=lambda: events.unsubscribe(queue))

    routes = [
        Route("/api/today", api(today_model)),
        Route("/api/review", api(review_model)),
        Route("/api/changes", api(changes_model)),
        Route("/api/health", api(health_model)),
        Route("/api/tree", api(tree_model)),
        Route("/api/events", sse),
        Route("/api/decide", decide_route, methods=["POST"]),
        Route("/api/revert", revert_route, methods=["POST"]),
        Route("/api/ingest", ingest_route, methods=["POST"]),
        Route("/api/note", note_get_route),
        Route("/api/note", note_put_route, methods=["POST"]),
        Route("/api/task/done", task_done_route, methods=["POST"]),
        Route("/api/task/due", task_due_route, methods=["POST"]),
        Route("/api/task/plan", task_plan_route, methods=["POST"]),
        Route("/api/pass", pass_route, methods=["POST"]),
        Route("/api/cancel", cancel_route, methods=["POST"]),
        Route("/api/chat", chat_send_route, methods=["POST"]),
        Route("/api/chat/confirm", chat_confirm_route, methods=["POST"]),
        Route("/api/chat/events", chat_events),
    ]
    if WEB_DIST.is_dir():
        routes.append(Mount("/", StaticFiles(directory=WEB_DIST, html=True)))
    else:
        routes.append(Route("/", lambda request: PlainTextResponse(
            "tezcat dashboard: frontend not built — run `npm install && npm run build` in web/. "
            "The API is live under /api/*; the vault stays fully usable from the editor meanwhile.")))

    @contextlib.asynccontextmanager
    async def lifespan(app):
        """Shutdown must close the chat session: the pane's Agent spawns
        MCP servers as child processes on the first turn, and a server
        exit that skips this orphans a tezcat-vault-mcp (plus
        tezcat-files-mcp and any HTTP sessions) and leaks HARNESS_CHAT_ID
        into the environment. Close the session, then stop the engine
        thread."""
        yield
        await chat.close()
        engine.shutdown()

    app = Starlette(routes=routes, lifespan=lifespan, middleware=[
        # The rebinding gate, on EVERY route: guard_write covers the
        # write verbs, but a DNS-rebound page reads /api/note and
        # /api/tree as same-origin GETs — the localhost bind alone
        # cannot tell a rebound request from a local one; the Host
        # header can.
        Middleware(TrustedHostMiddleware,
                   allowed_hosts=["127.0.0.1", "localhost", "::1"]),
    ])
    # A getter, because cancel(chat) rebinds the session: tests need the
    # CURRENT one to prove the swap happened.
    app.state.chat = lambda: chat
    return app


def main() -> None:
    from .bootstrap import load_runtime

    parser = argparse.ArgumentParser(
        prog="tezcat-ui",
        description="The owned dashboard: live views + SSE, decide/revert buttons")
    parser.add_argument("-v", "--vault", help="vault path (default: config, or $TEZCAT_VAULT_PATH)")
    parser.add_argument("--port", type=int, default=8399)
    args = parser.parse_args()
    _config, vault, _servers = load_runtime(vault=args.vault)

    import uvicorn

    print(f"tezcat dashboard: http://127.0.0.1:{args.port}  (vault: {vault})")
    uvicorn.run(build_app(vault), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
