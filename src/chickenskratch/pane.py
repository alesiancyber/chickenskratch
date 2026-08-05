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
# Slack over the derived budget below — scheduling, the teardown itself.
TEARDOWN_SLACK_S = 10.0


def teardown_budget(profile=None) -> float:
    """How long a stopped session may reasonably take to unwind.

    Derived, not picked. A stopped turn ends at its next checkpoint, and
    the agent loop has exactly two: between model calls and between tool
    calls. So the worst case is one in-flight completion plus one
    in-flight tool call, and both are bounded — by the profile's
    `request_timeout_s` and by mcp_client.CALL_TIMEOUT. A hardcoded 30s
    here was shorter than a single local-model reply, so it expired on
    every ordinary stop and took the cancel path below with it."""
    from .mcp_client import CALL_TIMEOUT
    from .profiles import Profile

    request = getattr(profile, "request_timeout_s", None) or Profile.request_timeout_s
    return float(request) + CALL_TIMEOUT + TEARDOWN_SLACK_S


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
        if self._worker is not None and not self._worker.done():
            return
        if self.agent is not None:
            # A worker that ended WITHOUT the close sentinel still owns the
            # agent's MCP transports — their cancel scopes belong to that
            # task, and calling them from a replacement raises "exit cancel
            # scope in a different task". Drop the agent so the new worker
            # builds its own; the old one's servers are unreachable either
            # way, and pretending otherwise turns a leak into a wedge.
            print(f"chat session {self.chat_id}: worker ended without teardown; "
                  "rebuilding the agent for the next turn")
            self.agent = None
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

    async def close(self, timeout: float | None = None) -> None:
        """Tear the session down: deny any parked gate, then hand the
        worker a close sentinel and let IT exit the MCP transports.

        The wait NEVER escalates to cancelling the worker. That looks
        like the obvious backstop and is the opposite of one: the
        transports are anyio cancel scopes entered by the worker task,
        but they live on the client object, not on the worker's call
        stack — so cancelling the worker doesn't unwind them, it strands
        them. Measured: anyio raises "Attempted to exit cancel scope in a
        different task", the stdio servers this session spawned never see
        their stdin close, and one chickenskratch-vault-mcp plus one
        chickenskratch-files-mcp outlive the session for as long as this
        process runs. Every stop press leaked another pair.

        So the sentinel is the only teardown there is, and a slow one
        costs patience rather than a leaked process. `timeout` bounds how
        long we WATCH, never what the worker does: past it we stop
        waiting and the worker still finishes its turn and still closes
        its servers. None derives the bound from the turn's own
        checkpoints (teardown_budget); the shutdown path passes a short
        one on purpose, because there the process is about to exit and
        exiting closes the pipes for us."""
        # A closing session takes no more turns: send() checks this, so
        # nothing can start a replacement worker beside the one that
        # still owns the agent's transports.
        self.stopped = True
        for fut in self.pending.values():
            if not fut.done():
                fut.set_result(False)
        worker = self._worker
        if worker is None or worker.done():
            self._worker = None
            return
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._inbox.put((None, done))
        budget = timeout if timeout is not None else teardown_budget(
            getattr(self.agent, "profile", None))
        try:
            await asyncio.wait_for(done, budget)
        except TimeoutError:
            # Cancelling `done` here touches this waiter only — the worker
            # holds its own reference and _run_worker guards on
            # done.done() before setting a result.
            print(f"chat session {self.chat_id}: still unwinding after "
                  f"{budget:g}s — its MCP servers close when it finishes")
            return
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
