"""The engine's own thread and event loop.

An engine pass is `async def` but barely awaits: it is git subprocesses,
whole-vault walks, difflib, and a synchronous worker-health probe, with
`await` points only around the LLM draft calls. The decisions fast lane
has none at all. Run on the HTTP server's loop — which is what the
dashboard's decide / ingest / pass routes were doing — that means the
whole server stops: no SSE ticks, no other requests, no static files.
Measured on a two-note vault, one decision froze the loop for 279ms
across zero yields; the surface renderers walk every note, so it grows
with the vault, not with the work.

So passes get their own loop on their own thread, and the routes await
the result across the boundary. A dedicated long-lived loop rather than
`asyncio.to_thread` per pass, for two reasons:

- the worker OpenAI client (and its httpx connection pool) is a module
  global; pools bind to the loop that first used them, so a fresh loop
  per pass would strand connections and eventually raise "event loop is
  closed" mid-draft;
- one loop means passes serialize naturally, which is what the pass
  flock wants anyway.

Events cross back on their own: fanout.publish uses call_soon_threadsafe
against each listener's recorded loop, which is exactly this case.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_guard = threading.Lock()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _thread
    with _guard:
        if _loop is not None and _thread is not None and _thread.is_alive():
            return _loop
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=_serve, args=(loop,), name="tezcat-engine", daemon=True)
        thread.start()
        _loop, _thread = loop, thread
        return loop


def _serve(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


async def run(coro: Coroutine):
    """Run `coro` on the engine loop and await its result here. The
    caller's loop stays free the whole time — that is the entire point."""
    future = asyncio.run_coroutine_threadsafe(coro, _ensure_loop())
    return await asyncio.wrap_future(future)


def spawn(coro: Coroutine) -> None:
    """Fire and forget on the engine loop: for a full pass, whose answer
    is the SSE pass_complete event rather than an HTTP response."""
    asyncio.run_coroutine_threadsafe(coro, _ensure_loop())


def shutdown(timeout: float = 5.0) -> None:
    """Stop the engine thread. Best-effort and idempotent — called from
    the server's lifespan shutdown; a pass still in flight holds the pass
    flock, which the next process re-acquires cleanly either way."""
    global _loop, _thread
    with _guard:
        loop, thread = _loop, _thread
        _loop, _thread = None, None
    if loop is None:
        return
    loop.call_soon_threadsafe(loop.stop)
    if thread is not None:
        thread.join(timeout=timeout)
