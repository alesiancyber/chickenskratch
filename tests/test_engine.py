"""The engine thread: passes run off the HTTP loop.

An engine pass is `async def` but barely awaits — git subprocesses, whole
vault walks, difflib — and the decisions lane has no await at all. Run on
the server's loop it froze the whole server, SSE included. These tests
pin the mechanism, not the symptom.
"""

import asyncio
import threading
import time

from chickenskratch import engine


def test_a_blocking_coroutine_leaves_the_calling_loop_free():
    async def scenario():
        ticks = []

        async def heartbeat():          # stands in for the SSE pump
            while True:
                await asyncio.sleep(0.01)
                ticks.append(1)

        hb = asyncio.ensure_future(heartbeat())

        async def blocking():           # stands in for a pass: no awaits
            time.sleep(0.3)
            return threading.current_thread().name

        where = await engine.run(blocking())
        hb.cancel()
        return where, len(ticks)

    where, ticks = asyncio.run(scenario())
    assert where == "chickenskratch-engine", "the pass ran on the caller's thread"
    assert ticks > 10, f"the calling loop only ticked {ticks}x — it was blocked"
    engine.shutdown()


def test_passes_serialize_on_the_one_engine_loop():
    """One loop means one pass at a time, which is what the pass flock
    wants anyway — and what keeps the worker client's connection pool
    bound to a single, living loop."""
    order = []

    async def scenario():
        async def job(tag):
            order.append(("start", tag))
            time.sleep(0.05)
            order.append(("end", tag))

        await asyncio.gather(engine.run(job("a")), engine.run(job("b")))

    asyncio.run(scenario())
    # no interleaving: every start is immediately followed by its own end
    assert [t for _, t in order[:2]] == [order[0][1]] * 2
    assert [t for _, t in order[2:]] == [order[2][1]] * 2
    engine.shutdown()


def test_shutdown_is_idempotent_and_restartable():
    engine.shutdown()
    engine.shutdown()   # no-op, not a crash

    async def scenario():
        return await engine.run(asyncio.sleep(0, result="ok"))

    assert asyncio.run(scenario()) == "ok"   # a new thread spins up on demand
    engine.shutdown()
