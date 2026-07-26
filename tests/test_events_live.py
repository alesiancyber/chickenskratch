"""The live event bus: in-process events reach listeners immediately.

The file remains the durable record and the only channel other processes
have. This covers the addition: a listener on this loop gets the record
without waiting for a poll, and can skip its own record when it later
tails the file.
"""

import asyncio

import pytest

from harness import events


@pytest.fixture()
def anchored(tmp_path, monkeypatch):
    monkeypatch.setattr("harness.events.ROOT", tmp_path)
    return "/some/vault"


def test_emit_reaches_a_live_listener_without_polling(anchored):
    async def scenario():
        q = events.subscribe()
        try:
            events.emit(anchored, "pass_complete", {"pending": 3})
            rec = await asyncio.wait_for(q.get(), 1.0)   # no poll interval
            assert rec["event"] == "pass_complete"
            assert rec["pending"] == 3
        finally:
            events.unsubscribe(q)

    asyncio.run(scenario())


def test_live_record_carries_the_span_it_wrote(anchored):
    """The span lets a listener advance past its own record instead of
    re-delivering it from the file tail."""
    async def scenario():
        q = events.subscribe()
        try:
            events.emit(anchored, "vault_changed", {"reason": "task_done"})
            rec = await asyncio.wait_for(q.get(), 1.0)
            start, end = rec["_span"]
            assert start == 0
            assert end == events.events_path(anchored).stat().st_size
            # and the file tail over exactly that span yields the same record
            off, recs = events.read_new(anchored, 0)
            assert off == end and len(recs) == 1
            assert recs[0]["event"] == "vault_changed"
        finally:
            events.unsubscribe(q)

    asyncio.run(scenario())


def test_unsubscribe_stops_delivery(anchored):
    """Browsers reconnect SSE routinely; a listener that outlives its
    stream would accumulate one queue per reconnect."""
    async def scenario():
        q = events.subscribe()
        events.unsubscribe(q)
        events.emit(anchored, "pass_complete", {})
        await asyncio.sleep(0.05)
        assert q.empty()

    asyncio.run(scenario())


def test_emit_still_works_with_no_listeners(anchored):
    """The CLI and cron emit into a process nobody is listening in."""
    events.emit(anchored, "pass_complete", {"pending": 0})
    _off, recs = events.read_new(anchored, 0)
    assert [r["event"] for r in recs] == ["pass_complete"]
