"""The escape-hatch pane's server side.

The frozen chat escape hatch, homed where its tool activity is
visible — principle 7's second home, under the same contract as the
CLI: this module hosts the SAME Agent loop `chickenskratch` runs (same
profile resolution, same server allowlists, same write gate, same
skills), with its hooks pointed at an event queue instead of a
terminal:

- `log` / `log_result`  -> visible tool rows, expandable to the raw
                           result (MCP made legible)
- `confirm`             -> an Allow/Deny future the UI resolves; every
                           verdict lands in the corpus via
                           record_confirm WITH the full trajectory

Transcripts stay scratch: history lives in the Agent's in-memory
messages and dies with the session — the vault is the archive. The
session injects HARNESS_CHAT_ID into ITS OWN MCP servers' env, so
vault snapshots made in-pane carry the Chat-Id trailer (revert
attribution). Scoped to those child processes, never os.environ: the
engine shares this process, and a process-global var would stamp the
pane's Chat-Id onto engine commits.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid

from chickenskratch_vault import ids

from .corpus import record_confirm
from .fanout import Fanout

CONFIRM_TIMEOUT = 600  # seconds; silent expiry = deny, marked timed_out
QUEUE_MAX = 500


class ChatSession:
    """One user's pane session: an Agent, an event fanout, and the
    pending Allow/Deny futures. `agent_factory` is the test seam — an
    async callable returning an entered Agent-shaped object."""

    def __init__(self, vault_path: str, agent_factory=None):
        self.vault_path = vault_path
        self.chat_id = ids.stamp_id("c")
        # Fanout, not one shared queue: every SSE stream gets its own feed,
        # so a second tab (or the overlap while a browser reconnects) sees
        # the whole turn instead of stealing half of it. See fanout.py.
        self.events = Fanout(QUEUE_MAX)
        self.pending: dict[str, asyncio.Future] = {}
        # Session-local, not the process-global cancel module: stop() ends
        # THIS session, and the server replaces it immediately — a shared
        # flag would let the replacement's first turn clear a stop the old
        # session's still-unwinding turn depends on.
        self.stopped = False
        self.agent = None
        self._factory = agent_factory or self._default_agent
        # Every Agent/MCP interaction happens inside ONE long-lived worker
        # task: the MCP SDK's transports are anyio cancel scopes bound to
        # the task that entered them, so per-request tasks would rot the
        # sessions unrecoverably. The worker also serializes turns, like
        # a terminal.
        self._inbox: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None

    # ---- hooks the Agent loop calls ----

    def _emit(self, event: dict) -> None:
        self.events.publish(event)

    def _log(self, name: str, args: dict) -> None:
        self._emit({"type": "tool", "name": name, "args": args})

    def _log_result(self, name: str, args: dict, text: str) -> None:
        self._emit({"type": "tool_result", "name": name, "result": text})

    async def _confirm(self, name: str, args: dict) -> bool:
        cid = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[cid] = fut
        self._emit({"type": "confirm_request", "id": cid, "name": name, "args": args})
        timed_out = False
        try:
            approved = bool(await asyncio.wait_for(fut, CONFIRM_TIMEOUT))
        except TimeoutError:
            approved, timed_out = False, True
        finally:
            self.pending.pop(cid, None)
        # self.agent can be None here when close() settled the gate;
        # the verdict still gets recorded, just without a trajectory.
        model = getattr(getattr(self.agent, "profile", None), "model", "")
        messages = list(getattr(self.agent, "messages", None) or [])
        record_confirm(self.chat_id, model, messages, name, args, approved,
                       timed_out=timed_out)  # swallows its own failures
        self._emit({"type": "confirm_resolved", "id": cid, "approved": approved})
        return approved

    # ---- the UI's verbs ----

    def respond(self, cid: str, allow: bool) -> bool:
        """Resolve one Allow/Deny chip. False = unknown/expired id."""
        fut = self.pending.get(cid)
        if fut is None or fut.done():
            return False
        fut.set_result(bool(allow))
        return True

    def stop(self) -> None:
        """The pane's stop button: this session is over. Marks the session
        dead (the agent loop unwinds at its next checkpoint — the in-flight
        model call still has to return, bounded by the profile's timeouts)
        and denies any unanswered write gate, since that IS a checkpoint
        the turn is currently parked on. The server does not wait for the
        unwind: it replaces the session immediately (ui.cancel_route), so
        the user gets a fresh chat now, not when the old turn finishes."""
        self.stopped = True
        for fut in self.pending.values():
            if not fut.done():
                fut.set_result(False)
        self._emit({"type": "stopping"})

    async def send(self, text: str) -> None:
        """One chat turn; every observable lands on the event queue.
        Resolves when the turn finishes (the route fire-and-forgets it).
        A stopped session ignores sends — it's already being replaced,
        and its turn would cancel on the first checkpoint anyway."""
        if self.stopped:
            return
        self._ensure_worker()
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._inbox.put((text, done))
        await done

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._inbox = asyncio.Queue()
            self._worker = asyncio.create_task(self._run_worker())

    async def _run_worker(self) -> None:
        while True:
            text, done = await self._inbox.get()
            try:
                if text is None:  # close sentinel: tear down IN this task
                    if self.agent is not None:
                        try:
                            await self.agent.__aexit__(None, None, None)
                        except Exception:
                            pass
                        self.agent = None
                    return
                await self._turn(text)
            finally:
                if not done.done():
                    done.set_result(None)

    async def _turn(self, text: str) -> None:
        if self.agent is None:
            try:
                self.agent = await self._factory()
            except Exception as e:
                self._emit({"type": "error", "message": f"chat unavailable: {e}"})
                return
            self._emit({"type": "ready",
                        "model": getattr(self.agent.profile, "model", ""),
                        "servers": list(getattr(self.agent.profile, "servers", None) or [])})
        self._emit({"type": "user", "text": text})
        try:
            reply = await self.agent.run(text)
        except Exception as e:
            self._emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
            return
        self._emit({"type": "assistant", "content": reply})

    async def close(self) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_result(False)
        if self._worker is not None and not self._worker.done():
            done: asyncio.Future = asyncio.get_running_loop().create_future()
            await self._inbox.put((None, done))
            try:
                await asyncio.wait_for(done, 30)
            except TimeoutError:
                self._worker.cancel()
        self._worker = None

    # ---- the real Agent, exactly as the CLI builds it ----

    async def _default_agent(self):
        from .agent import Agent, app_note_link
        from .bootstrap import load_runtime
        from .skills import load_skills

        config, vault, servers = load_runtime(vault=self.vault_path)
        profile = config.profile(None)  # the default chat profile
        # The chat id rides into the MCP servers THIS session spawns, via
        # their own env — never os.environ. The engine runs in this same
        # process and rebuilds its vault singleton from the environment,
        # so a process-global var would stamp the pane's Chat-Id onto
        # engine commits: auto-applies misattributed to the chat in the
        # changes rows, and reverted engine actions filed against the
        # chat's trajectory in the corpus.
        servers = [dataclasses.replace(s, env={**s.env, "HARNESS_CHAT_ID": self.chat_id})
                   for s in servers]
        # note_link: this pane sits next to our own editor, so a cited
        # source opens there — never in another app.
        agent = Agent(profile, servers, log=self._log, confirm=self._confirm,
                      skills=load_skills(), vault=vault, log_result=self._log_result,
                      should_cancel=lambda: self.stopped,
                      note_link=app_note_link)
        await agent.__aenter__()
        return agent
