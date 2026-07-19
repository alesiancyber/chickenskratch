"""Lightweight local chat UI for the harness.

One Starlette app (already a dependency via mcp), one static HTML page,
no build step. Tool calls stream to the browser as SSE events while the
model works; gated write tools surface as approve/deny prompts in the
chat and block until answered.  Run with: uv run harness-web
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .agent import Agent
from .bootstrap import DEFAULT_MCP, DEFAULT_MODELS, ROOT, load_runtime
from .chats import ChatStore, transcript
from .mcp_client import ServerSpec
from .profiles import Config
from .skills import load_skills

STATIC = Path(__file__).parent / "static"
REPORTS_DIR = ROOT / "evals" / "reports"

CONFIRM_TIMEOUT = 600  # seconds before an unanswered approval is treated as deny


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
    ):
        self.model_name = model_name
        self.profile = config.profile(model_name)
        self.servers = servers
        self.on_save = on_save
        self.initial_messages = initial_messages
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

    async def _confirm(self, name: str, args: dict) -> bool:
        confirm_id = uuid.uuid4().hex[:12]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[confirm_id] = future
        self._out.put_nowait(
            {"type": "confirm", "confirm_id": confirm_id, "name": name, "args": args}
        )
        try:
            approved = await asyncio.wait_for(future, timeout=CONFIRM_TIMEOUT)
        except TimeoutError:
            approved = False
        finally:
            self.pending.pop(confirm_id, None)
        self._out.put_nowait(
            {"type": "confirm_result", "confirm_id": confirm_id, "approved": approved}
        )
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
                skills=load_skills(), on_delta=self._emit_delta,
            ) as agent:
                self.status = agent.client.status
                self.agent = agent
                if self.initial_messages:
                    # resume a saved chat, but with a freshly built system
                    # prompt (dates and skills may have changed since)
                    agent.messages = [agent.messages[0], *self.initial_messages[1:]]
                while True:
                    item = await self.inbox.get()
                    if item is None:
                        return
                    message, out, approvals = item
                    self._out = out
                    agent.confirm_writes_override = approvals
                    try:
                        final = await agent.run(message)
                        out.put_nowait({"type": "final", "content": final})
                        if self.on_save:
                            self.on_save(agent.messages)
                    except Exception as e:
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

    def __init__(self, config: Config, servers: list[ServerSpec], store: ChatStore):
        self.config = config
        self.servers = servers
        self.store = store
        self.worker: AgentWorker | None = None
        self.chat_id: str | None = None
        self.lock = asyncio.Lock()

    def _saver(self):
        chat_id = self.chat_id

        def save(messages: list[dict]) -> None:
            model = self.worker.model_name if self.worker else ""
            self.store.save(chat_id, model, messages)

        return save

    async def get_worker(self, model_name: str | None) -> AgentWorker:
        name = model_name or (self.worker.model_name if self.worker else self.config.default_model)
        if self.worker is None or self.worker.model_name != name or self.worker.failed:
            await self.reset()
            self.chat_id = self.store.new_id()
            self.worker = AgentWorker(self.config, name, self.servers, on_save=self._saver())
        return self.worker

    async def open_chat(self, chat_id: str) -> dict | None:
        data = self.store.load(chat_id)
        if data is None:
            return None
        await self.reset()
        self.chat_id = chat_id
        model = data["model"] if data["model"] in self.config.models else self.config.default_model
        self.worker = AgentWorker(
            self.config, model, self.servers,
            on_save=self._saver(), initial_messages=data["messages"],
        )
        return {"id": chat_id, "model": model, "transcript": transcript(data["messages"])}

    async def reset(self) -> None:
        if self.worker is not None:
            await self.worker.stop()
            self.worker = None
        self.chat_id = None


def build_app(config: Config, servers: list[ServerSpec], vault: str) -> Starlette:
    store = ChatStore()
    store.prune()
    manager = Manager(config, servers, store)
    reach_cache: dict = {"ts": 0.0, "map": {}}

    async def endpoint_reachability() -> dict[str, bool]:
        """Which profiles' endpoints answer right now (cached 30s). Lets the
        UI mark dead backends instead of letting a chat walk into one."""
        import time

        import httpx

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

    async def index(request):
        return FileResponse(STATIC / "index.html")

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

        approvals = data.get("approvals")
        if not isinstance(approvals, bool):
            approvals = None  # use the profile default

        async def stream():
            async with manager.lock:
                worker = await manager.get_worker(data.get("model"))
                async for event in worker.ask(message, approvals):
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

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
        from obsidian_mcp.vault import Vault

        return Vault(vault)

    async def capture_start(request):
        data = await request.json()
        v = _vault()
        now = dt.datetime.now()
        title = re.sub(r'[\\/:*?"<>|]', "", (data.get("title") or "").strip()) or now.strftime("%H.%M")
        rel = f"Captures/{now.date().isoformat()} {title}.md"
        created = not v.resolve(rel).exists()
        if created:
            v.snapshot_before("capture")
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
        if path:
            v = _vault()
            if lines == 0 and capture["created"]:
                # Nothing captured and we made the note: leave no litter.
                v.resolve(path).unlink(missing_ok=True)
                v.snapshot_after(f"discard empty capture {path}")
            else:
                v.snapshot_after(f"capture session {path}")
        capture.update(path=None, lines=0, created=False)
        return JSONResponse({"path": path, "lines": lines})

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
        ok, error = _vault().revert(sha)
        return JSONResponse({"ok": ok, "error": error}, status_code=200 if ok else 409)

    async def evals_page(request):
        return FileResponse(STATIC / "evals.html")

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

    @asynccontextmanager
    async def lifespan(app):
        yield
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

    print(f"vault: {vault}")
    print(f"servers: {', '.join(s.name + ('' if s.enabled else ' (disabled)') for s in servers)}")
    print(f"chat UI: http://{args.host}:{args.port}")
    uvicorn.run(build_app(config, servers, vault), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
