"""Lightweight local chat UI for the harness.

One Starlette app (already a dependency via mcp), one static HTML page,
no build step. Tool calls stream to the browser as SSE events while the
model works; gated write tools surface as approve/deny prompts in the
chat and block until answered.  Run with: uv run harness-web
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from obsidian_mcp.vault import Vault, VaultError

from .agent import Agent
from .backends import BackendManager
from .bootstrap import DEFAULT_MCP, DEFAULT_MODELS, ROOT, load_runtime
from .bugreports import save_report
from .chats import ChatStore, transcript
from .corpus import record_bug_report, record_confirm, record_revert
from .mcp_client import ServerSpec, load_mcp_presets
from .profiles import Config
from .skills import load_skills
from .stats import record_run, totals

STATIC = Path(__file__).parent / "static"
REPORTS_DIR = ROOT / "evals" / "reports"
CATALOG_PATH = ROOT / "data" / "tool_catalog.json"

CONFIRM_TIMEOUT = 600  # seconds before an unanswered approval is treated as deny


def load_catalog() -> dict:
    """Tool catalog {server: [{name, read_only}]} discovered on past connects.
    Lets the UI offer per-tool selection before this session has reconnected."""
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_catalog(seen: dict) -> None:
    """Merge freshly-connected servers into the catalog, keeping entries for
    servers this (possibly narrowed) session didn't connect."""
    if not seen:
        return
    cat = load_catalog()
    cat.update(seen)
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(json.dumps(cat, indent=2), encoding="utf-8")


class AgentWorker:
    """Owns one Agent inside a dedicated task.

    The MCP sessions must be entered and exited by the same task (anyio
    cancel scopes), so HTTP handlers never touch the Agent directly —
    they enqueue a message and read events back off a queue.
    """

    def __init__(
        self,
        config: Config,
        model_name: str,
        servers: list[ServerSpec],
        on_save=None,
        initial_messages: list[dict] | None = None,
        initial_stats: dict | None = None,
        server_filter: list[str] | None = None,
        tool_filter: list[str] | None = None,
        vault: str = "",
        chat_id: str = "",
    ):
        self.model_name = model_name
        self.chat_id = chat_id
        self.server_filter = server_filter
        self.tool_filter = tool_filter
        self.vault = vault
        profile = config.profile(model_name)
        if server_filter is not None:
            # session override from the UI: connect only these MCP servers,
            # shrinking the tool schemas the model carries every request
            profile = dataclasses.replace(profile, servers=server_filter)
        self.profile = profile
        self.servers = servers
        self.on_save = on_save
        self.initial_messages = initial_messages
        self.initial_stats = initial_stats
        self.agent: Agent | None = None
        self.failed = False
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.pending: dict[str, asyncio.Future] = {}
        self.status: dict = {}
        self._out: asyncio.Queue | None = None
        self.task = asyncio.create_task(self._run())

    def _emit_tool(self, name: str, args: dict) -> None:
        if self._out is not None:
            self._out.put_nowait({"type": "tool", "name": name, "args": args})

    def _emit_delta(self, text: str) -> None:
        if self._out is not None:
            self._out.put_nowait({"type": "delta", "text": text})

    def _stats_event(self, agent: Agent) -> dict:
        return {
            "type": "stats",
            "chat": agent.stats,
            "context_window": self.profile.context_tokens,
        }

    async def _confirm(self, name: str, args: dict) -> bool:
        confirm_id = uuid.uuid4().hex[:12]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[confirm_id] = future
        self._out.put_nowait(
            {"type": "confirm", "confirm_id": confirm_id, "name": name, "args": args}
        )
        timed_out = False
        try:
            approved = await asyncio.wait_for(future, timeout=CONFIRM_TIMEOUT)
        except TimeoutError:
            approved, timed_out = False, True
        finally:
            self.pending.pop(confirm_id, None)
        self._out.put_nowait(
            {"type": "confirm_result", "confirm_id": confirm_id, "approved": approved}
        )
        try:
            record_confirm(
                self.chat_id, self.model_name,
                self.agent.messages if self.agent else [],
                name, args, approved, timed_out=timed_out,
            )
        except OSError:
            pass  # corpus capture never breaks a live chat
        return approved

    def resolve(self, confirm_id: str, approve: bool) -> bool:
        future = self.pending.get(confirm_id)
        if future is None or future.done():
            return False
        future.set_result(approve)
        return True

    async def _run(self) -> None:
        try:
            async with Agent(
                self.profile, self.servers, log=self._emit_tool, confirm=self._confirm,
                skills=load_skills(), on_delta=self._emit_delta, vault=self.vault,
                tool_filter=self.tool_filter, chat_id=self.chat_id,
            ) as agent:
                self.status = agent.client.status
                self.agent = agent
                save_catalog(agent.client.catalog())  # remember tools for the picker
                if self.initial_messages:
                    # resume a saved chat, but with a freshly built system
                    # prompt (dates and skills may have changed since)
                    agent.messages = [agent.messages[0], *self.initial_messages[1:]]
                if self.initial_stats:
                    agent.stats.update(self.initial_stats)
                while True:
                    item = await self.inbox.get()
                    if item is None:
                        return
                    message, out, approvals = item
                    self._out = out
                    agent.confirm_writes_override = approvals
                    try:
                        final = await agent.run(message)
                        out.put_nowait(self._stats_event(agent))
                        out.put_nowait({"type": "final", "content": final})
                        if self.on_save:
                            self.on_save(agent.messages, agent.stats)
                        try:
                            record_run(self.model_name, agent.last_run_stats)
                        except OSError:
                            pass  # totals are cosmetic; never fail a chat over them
                    except Exception as e:
                        # A failed run may have killed the MCP sessions (stdio
                        # transports don't survive some cancellations). Mark the
                        # worker so the manager rebuilds it — resuming this chat
                        # from its last save — instead of serving a broken agent.
                        self.failed = True
                        out.put_nowait({"type": "error", "content": f"{type(e).__name__}: {e}"})
        except Exception as e:
            # Startup failed. Mark it so the manager builds a fresh worker on
            # the next message (the backend may have come back) instead of
            # serving this cached error forever.
            self.failed = True
            while True:
                item = await self.inbox.get()
                if item is None:
                    return
                item[1].put_nowait({"type": "error", "content": f"Agent failed to start: {e}"})

    async def ask(self, message: str, approvals: bool | None = None):
        out: asyncio.Queue = asyncio.Queue()
        await self.inbox.put((message, out, approvals))
        while True:
            event = await out.get()
            yield event
            if event["type"] in ("final", "error"):
                return

    async def stop(self) -> None:
        await self.inbox.put(None)
        await self.task


class Manager:
    """One active conversation at a time; saved chats can be reopened."""

    def __init__(self, config: Config, servers: list[ServerSpec], store: ChatStore, vault: str = ""):
        self.config = config
        self.servers = servers
        self.store = store
        self.vault = vault
        self.worker: AgentWorker | None = None
        self.chat_id: str | None = None
        # UI-chosen tool working-set (qualified tool names); None = defaults.
        # Sticky until changed — survives new chats and model switches.
        self.tool_override: list[str] | None = None
        self.lock = asyncio.Lock()

    def effective_filters(self, model_name: str | None) -> tuple[list[str] | None, list[str] | None]:
        """Resolve the sticky tool selection into (server_filter, tool_filter):
        connect only servers that own a selected tool, expose only those tools.
        None/None means profile defaults (connect all, expose all)."""
        prof = self.config.profile(model_name)
        enabled = [s.name for s in self.servers if s.enabled]
        default_servers = [n for n in enabled if prof.servers is None or n in prof.servers]
        if self.tool_override is None:
            return None, None
        catalog = load_catalog()
        owner = {t["name"]: srv for srv, tools in catalog.items() for t in tools}
        sel = [t for t in self.tool_override if owner.get(t) in default_servers]
        if not sel:  # selection stranded by a model switch — fall back to defaults
            return None, None
        return sorted({owner[t] for t in sel}), sel

    def _saver(self):
        chat_id = self.chat_id

        def save(messages: list[dict], stats: dict | None = None) -> None:
            model = self.worker.model_name if self.worker else ""
            self.store.save(chat_id, model, messages, stats)

        return save

    async def get_worker(self, model_name: str | None) -> AgentWorker:
        name = model_name or (self.worker.model_name if self.worker else self.config.default_model)
        server_filter, tool_filter = self.effective_filters(name)
        tools_changed = self.worker is not None and (
            self.worker.server_filter != server_filter or self.worker.tool_filter != tool_filter
        )
        if self.worker is None or self.worker.model_name != name or self.worker.failed or tools_changed:
            # Rebuild with fresh MCP sessions. A mid-conversation rebuild
            # (failure or tool-selection change) resumes the same chat from
            # its last saved state instead of dropping the conversation.
            resume, resume_stats, chat_id = None, None, None
            if (
                self.worker is not None
                and (self.worker.failed or tools_changed)
                and self.worker.model_name == name
                and self.chat_id
            ):
                data = self.store.load(self.chat_id)
                if data:
                    resume, resume_stats, chat_id = data["messages"], data.get("stats"), self.chat_id
            await self.reset()
            self.chat_id = chat_id or self.store.new_id()
            self.worker = AgentWorker(
                self.config, name, self.servers, on_save=self._saver(),
                initial_messages=resume, initial_stats=resume_stats,
                server_filter=server_filter, tool_filter=tool_filter, vault=self.vault,
                chat_id=self.chat_id,
            )
        return self.worker

    async def open_chat(self, chat_id: str) -> dict | None:
        data = self.store.load(chat_id)
        if data is None:
            return None
        await self.reset()
        self.chat_id = chat_id
        model = data["model"] if data["model"] in self.config.models else self.config.default_model
        server_filter, tool_filter = self.effective_filters(model)
        self.worker = AgentWorker(
            self.config, model, self.servers,
            on_save=self._saver(), initial_messages=data["messages"],
            initial_stats=data.get("stats"), server_filter=server_filter,
            tool_filter=tool_filter, vault=self.vault, chat_id=chat_id,
        )
        return {
            "id": chat_id,
            "model": model,
            "transcript": transcript(data["messages"]),
            "stats": data.get("stats") or {},
        }

    async def reset(self) -> None:
        if self.worker is not None:
            await self.worker.stop()
            self.worker = None
        self.chat_id = None


def build_app(
    config: Config,
    servers: list[ServerSpec],
    vault: str,
    presets: dict | None = None,
    watch: bool = False,
) -> Starlette:
    store = ChatStore()
    store.prune()
    manager = Manager(config, servers, store, vault)
    backend_mgr = BackendManager(config.backends)
    presets = presets or {}
    reach_cache: dict = {"ts": 0.0, "map": {}}

    async def endpoint_reachability() -> dict[str, bool]:
        """Which profiles' endpoints answer right now (cached 30s). Lets the
        UI mark dead backends instead of letting a chat walk into one."""
        if time.monotonic() - reach_cache["ts"] < 30 and reach_cache["map"]:
            return reach_cache["map"]
        by_url: dict[str, bool] = {}
        async with httpx.AsyncClient(timeout=1.5) as http:
            for url in {p.base_url for p in config.models.values()}:
                try:
                    await http.get(url.rstrip("/") + "/models")
                    by_url[url] = True
                except httpx.HTTPError:
                    by_url[url] = False
        result = {name: by_url[p.base_url] for name, p in config.models.items()}
        reach_cache.update(ts=time.monotonic(), map=result)
        return result

    def _page(name: str) -> FileResponse:
        # no-cache = revalidate every load (cheap 304s via ETag). Without it
        # browsers heuristically cache the app shell, and a stale copy keeps
        # running old JS long after a fix lands (e.g. the sanitizer change
        # that un-broke obsidian:// citation links).
        return FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})

    async def index(request):
        return _page("index.html")

    def _mcp_info() -> dict:
        """Per-tool selection state grouped by server, plus presets — the UI's
        whole picture for the tool working-set. Tools come from the on-disk
        catalog so selection works before this session reconnects."""
        prof = config.profile(manager.worker.model_name if manager.worker else None)
        enabled = [s.name for s in servers if s.enabled]
        default_servers = [n for n in enabled if prof.servers is None or n in prof.servers]
        catalog = load_catalog()
        live = manager.worker.status if manager.worker and manager.worker.status else {}
        override = set(manager.tool_override) if manager.tool_override is not None else None
        rows = []
        for s in servers:
            tools = []
            for t in sorted(catalog.get(s.name, []), key=lambda t: t["name"]):
                selected = t["name"] in override if override is not None else s.name in default_servers
                tools.append({"name": t["name"], "read_only": t.get("read_only", False), "selected": selected})
            rows.append({
                "name": s.name,
                "config_enabled": s.enabled,
                "available": s.name in default_servers,
                "connected": (live.get(s.name) or {}).get("state") == "connected",
                "tools": tools,
            })
        return {"servers": rows, "presets": presets, "has_catalog": bool(catalog)}

    async def get_config(request):
        if manager.worker and manager.worker.status:
            server_info = manager.worker.status
        else:
            server_info = {
                s.name: {"state": "enabled" if s.enabled else "disabled"} for s in servers
            }
        return JSONResponse(
            {
                "models": list(config.models),
                "default": config.default_model,
                "current": manager.worker.model_name if manager.worker else None,
                "vault": vault,
                "servers": server_info,
                "confirm_writes": {name: p.confirm_writes for name, p in config.models.items()},
                "context_windows": {name: p.context_tokens for name, p in config.models.items()},
                "mcp": _mcp_info(),
                "reachable": await endpoint_reachability(),
                # fresh from disk so skill edits show in the menu immediately
                "skills": [
                    {"name": s.name, "description": s.description} for s in load_skills()
                ],
            }
        )

    async def chat(request):
        data = await request.json()
        message = (data.get("message") or "").strip()
        if not message:
            return JSONResponse({"error": "empty message"}, status_code=400)

        model = data.get("model")
        if model and model not in config.models:
            # Reject before the SSE stream opens: an exception mid-stream
            # would just drop the connection with no error the UI can show.
            return JSONResponse(
                {"error": f"unknown model {model!r}. Available: {', '.join(config.models)}"},
                status_code=400,
            )

        approvals = data.get("approvals")
        if not isinstance(approvals, bool):
            approvals = None  # use the profile default

        async def stream():
            try:
                async with manager.lock:
                    worker = await manager.get_worker(model)
                    async for event in worker.ask(message, approvals):
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except Exception as e:
                # Last-resort net: surface the failure as an event instead of
                # severing the stream with no explanation.
                err = {"type": "error", "content": f"{type(e).__name__}: {e}"}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    async def confirm(request):
        data = await request.json()
        worker = manager.worker
        ok = bool(
            worker and worker.resolve(str(data.get("id")), bool(data.get("approve")))
        )
        return JSONResponse({"ok": ok})

    async def reset(request):
        async with manager.lock:
            await manager.reset()
        return JSONResponse({"ok": True})

    # Capture mode: model-free by design. Lines append verbatim to a note in
    # the vault (same Vault module the MCP server uses, so git snapshots
    # apply); the model only enters when the session ends and the UI invokes
    # the meeting-capture skill on the finished note.
    capture: dict = {"path": None, "lines": 0, "created": False}

    def _vault():
        return Vault(vault)

    async def capture_start(request):
        data = await request.json()
        v = _vault()
        now = dt.datetime.now()
        title = re.sub(r'[\\/:*?"<>|]', "", (data.get("title") or "").strip()) or now.strftime("%H.%M")
        rel = f"Captures/{now.date().isoformat()} {title}.md"
        created = not v.resolve(rel).exists()
        if created:
            try:
                v.snapshot_before("capture")
            except VaultError as e:
                return JSONResponse({"error": str(e)}, status_code=500)
            v.write(rel, f"# Capture: {title}\n\n_{now.strftime('%A %Y-%m-%d %H:%M')}_\n\n")
        capture.update(path=rel, lines=0, created=created)
        return JSONResponse({"path": rel})

    async def capture_append(request):
        if not capture["path"]:
            return JSONResponse({"error": "no active capture"}, status_code=400)
        data = await request.json()
        line = (data.get("text") or "").rstrip()
        if line:
            v = _vault()
            p = v.resolve(capture["path"])
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            capture["lines"] += 1
        return JSONResponse({"ok": True, "path": capture["path"]})

    async def capture_stop(request):
        path, lines = capture["path"], capture["lines"]
        error = ""
        if path:
            v = _vault()
            try:
                if lines == 0 and capture["created"]:
                    # Nothing captured and we made the note: leave no litter.
                    v.resolve(path).unlink(missing_ok=True)
                    v.snapshot_after(f"discard empty capture {path}")
                else:
                    v.snapshot_after(f"capture session {path}")
            except VaultError as e:
                error = str(e)  # captured text is on disk; only the snapshot failed
        capture.update(path=None, lines=0, created=False)
        return JSONResponse({"path": path, "lines": lines, **({"error": error} if error else {})})

    async def chats_list(request):
        return JSONResponse({"chats": store.list(), "current": manager.chat_id})

    async def chats_current(request):
        worker = manager.worker
        if worker is None or worker.agent is None:
            return JSONResponse({"id": None, "transcript": []})
        return JSONResponse(
            {
                "id": manager.chat_id,
                "model": worker.model_name,
                "transcript": transcript(worker.agent.messages),
                "stats": worker.agent.stats,
            }
        )

    async def chats_open(request):
        data = await request.json()
        async with manager.lock:
            result = await manager.open_chat(str(data.get("id") or ""))
        if result is None:
            return JSONResponse({"error": "chat not found"}, status_code=404)
        return JSONResponse(result)

    async def history(request):
        return JSONResponse({"commits": _vault().history(15)})

    async def revert(request):
        data = await request.json()
        sha = str(data.get("sha") or "")
        if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
            return JSONResponse({"ok": False, "error": "invalid sha"}, status_code=400)
        v = _vault()
        commit_message = v.commit_message(sha)
        ok, error = v.revert(sha)
        if ok:
            # A revert is a negative label on the chat that made the commit,
            # found via its Chat-Id trailer. Chat already pruned (or commit
            # untrailered: manual edit, pre-feature) => nothing to train on,
            # skip silently — the accepted gap in DESIGN-training-corpus.md.
            m = re.search(r"^Chat-Id: (\S+)$", commit_message, re.M)
            chat = store.load(m.group(1)) if m else None
            if chat:
                try:
                    record_revert(chat["id"], chat["model"], chat["messages"], sha, commit_message)
                except OSError:
                    pass
        return JSONResponse({"ok": ok, "error": error}, status_code=200 if ok else 409)

    async def evals_page(request):
        return _page("evals.html")

    async def stats_page(request):
        return _page("stats.html")

    async def get_stats(request):
        return JSONResponse({"models": totals()})

    async def report_issue(request):
        """Save the current conversation as a bug report for later review."""
        data = await request.json()
        worker = manager.worker
        agent = worker.agent if worker else None
        messages = agent.messages if agent else []
        try:
            path = save_report(
                description=str(data.get("description") or ""),
                model=worker.model_name if worker else "",
                messages=messages,
                stats=agent.stats if agent else None,
                servers=worker.status if worker else None,
            )
        except OSError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        try:
            record_bug_report(
                manager.chat_id or "", worker.model_name if worker else "",
                messages, str(data.get("description") or ""), str(path),
            )
        except OSError:
            pass
        try:
            shown = str(path.relative_to(ROOT))
        except ValueError:
            shown = str(path)
        return JSONResponse({"ok": True, "path": shown})

    async def mcp_select(request):
        """Choose the tool working-set the agent exposes (context + selection
        control). Applies from the next message via a rebuild that resumes the
        chat. `tools: null` resets to profile defaults."""
        data = await request.json()
        tools = data.get("tools")
        if tools is None:
            manager.tool_override = None
            return JSONResponse({"ok": True, "mcp": _mcp_info()})
        if not isinstance(tools, list) or not tools:
            return JSONResponse({"ok": False, "error": "select at least one tool"}, status_code=400)
        catalog = load_catalog()
        known = {t["name"] for row in catalog.values() for t in row}
        unknown = [t for t in tools if t not in known]
        if known and unknown:
            return JSONResponse({"ok": False, "error": f"unknown tools: {unknown}"}, status_code=400)
        manager.tool_override = sorted(dict.fromkeys(tools))  # dedupe, stable order
        return JSONResponse({"ok": True, "mcp": _mcp_info()})

    # --- local model-backend control (config-declared commands only) ---

    async def servers_list(request):
        out = []
        async with httpx.AsyncClient(timeout=1.5) as http:
            for name, spec in config.backends.items():
                try:
                    await http.get(spec.url + "/models")
                    reachable = True
                except httpx.HTTPError:
                    reachable = False
                pid = backend_mgr.managed_pid(name)
                out.append({
                    "name": name, "url": spec.url, "reachable": reachable,
                    "managed": pid is not None, "pid": pid,
                    # which profiles ride on this backend, for the UI
                    "profiles": [n for n, p in config.models.items()
                                 if p.base_url.rstrip("/") == spec.url],
                })
        return JSONResponse({"servers": out})

    async def servers_start(request):
        data = await request.json()
        name = str(data.get("name") or "")
        if name not in config.backends:
            return JSONResponse({"ok": False, "message": f"unknown backend {name!r}"}, status_code=404)
        ok, message = backend_mgr.start(name)
        reach_cache["ts"] = 0  # model picker re-probes immediately
        return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 409)

    async def servers_stop(request):
        data = await request.json()
        name = str(data.get("name") or "")
        if name not in config.backends:
            return JSONResponse({"ok": False, "message": f"unknown backend {name!r}"}, status_code=404)
        # stop() blocks up to its kill grace period; keep the event loop free
        ok, message = await asyncio.to_thread(backend_mgr.stop, name)
        reach_cache["ts"] = 0
        return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 409)

    async def get_evals(request):
        runs = []
        for f in sorted(REPORTS_DIR.glob("*.json")) if REPORTS_DIR.is_dir() else []:
            stem = f.stem  # "YYYY-MM-DD-HHMM-<profile>"
            try:
                results = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            runs.append(
                {
                    "stamp": stem[:15],
                    "profile": stem[16:],
                    "passed": sum(r["passed"] for r in results),
                    "total": len(results),
                    "seconds": round(sum(r["seconds"] for r in results), 1),
                    "results": results,
                }
            )
        return JSONResponse({"runs": runs})

    async def _watch_loop():
        # Review.md is the dashboard (DESIGN-vault.md §5): a checked box is
        # the approval, and this poll is what notices it while the server
        # runs. One cheap stat per tick; a full pass only when the file
        # changed. `harness-watch` covers the server-not-running case.
        from .watcher import Watcher

        w = Watcher(vault)
        review = Path(vault) / w.review_rel
        last = None
        while True:
            try:
                mtime = review.stat().st_mtime if review.exists() else None
                if last is None or mtime != last:
                    await w.run_once()
                    mtime = review.stat().st_mtime if review.exists() else None
                last = mtime
            except Exception:
                pass  # the watcher must never take the chat server down
            await asyncio.sleep(5)

    @asynccontextmanager
    async def lifespan(app):
        watch_task = asyncio.create_task(_watch_loop()) if watch else None
        yield
        if watch_task:
            watch_task.cancel()
        await manager.reset()

    return Starlette(
        routes=[
            Route("/", index),
            Route("/api/config", get_config),
            Route("/api/chat", chat, methods=["POST"]),
            Route("/api/confirm", confirm, methods=["POST"]),
            Route("/api/reset", reset, methods=["POST"]),
            Route("/evals", evals_page),
            Route("/api/evals", get_evals),
            Route("/stats", stats_page),
            Route("/api/stats", get_stats),
            Route("/api/report", report_issue, methods=["POST"]),
            Route("/api/servers", servers_list),
            Route("/api/servers/start", servers_start, methods=["POST"]),
            Route("/api/servers/stop", servers_stop, methods=["POST"]),
            Route("/api/mcp", mcp_select, methods=["POST"]),
            Route("/api/chats", chats_list),
            Route("/api/chats/current", chats_current),
            Route("/api/chats/open", chats_open, methods=["POST"]),
            Route("/api/history", history),
            Route("/api/revert", revert, methods=["POST"]),
            Route("/api/capture/start", capture_start, methods=["POST"]),
            Route("/api/capture/append", capture_append, methods=["POST"]),
            Route("/api/capture/stop", capture_stop, methods=["POST"]),
            Mount("/static", StaticFiles(directory=STATIC)),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="harness-web", description="Local chat UI for the Obsidian harness")
    parser.add_argument("-v", "--vault", help="vault path (default: config, or $OBSIDIAN_VAULT_PATH)")
    parser.add_argument("-c", "--config", default=str(DEFAULT_MODELS), help="path to models.yaml")
    parser.add_argument("--mcp-config", default=str(DEFAULT_MCP), help="path to mcp.yaml")
    parser.add_argument("-p", "--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    config, vault, servers = load_runtime(args.config, args.mcp_config, args.vault)
    presets = load_mcp_presets(args.mcp_config)

    print(f"vault: {vault}")
    print(f"servers: {', '.join(s.name + ('' if s.enabled else ' (disabled)') for s in servers)}")
    print(f"chat UI: http://{args.host}:{args.port}")
    uvicorn.run(
        build_app(config, servers, vault, presets, watch=True),
        host=args.host, port=args.port, log_level="warning",
    )


if __name__ == "__main__":
    main()
