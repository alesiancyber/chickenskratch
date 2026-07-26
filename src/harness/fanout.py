"""Bounded in-process broadcast to live listeners.

One mechanism, two users: the engine's event stream (events.py) and the
escape-hatch pane's turn events (pane.py). Both want the same three
properties, and each had grown its own copy — but the pane's copy was a
single SHARED queue that every SSE stream drained. `get()` removes the
record, so a second listener didn't get a second copy, it stole half the
first one's: two browser tabs interleaved one transcript into halves,
and because browsers drop and re-open SSE connections routinely (we
even ask them to, with `retry: 2000`), a live turn's tool rows and
Allow/Deny chips could land on the dying stream and simply vanish.

The three properties, held here once:

- one queue PER listener, so N listeners each see every record;
- bounded, dropping the OLDEST on overflow — a live UI wants the newest,
  and a stalled listener must never grow memory without limit;
- publishable from ANY thread: emitters run on the engine loop, on the
  HTTP loop, and in threadpool workers, and putting to an asyncio.Queue
  from another thread is not safe.

Listeners see records published from the moment they subscribe. That is
the right default for both users: the engine's durable record is its
JSONL file (which the SSE route tails to catch up), and pane transcripts
are scratch by design.
"""

from __future__ import annotations

import asyncio
import contextlib


class Fanout:
    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self._subscribers: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}

    def subscribe(self) -> asyncio.Queue:
        """A private feed for a stream running on the current loop. The
        caller MUST unsubscribe when its stream ends, however it ends."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self.maxsize)
        self._subscribers[q] = asyncio.get_running_loop()
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.pop(q, None)

    @property
    def listeners(self) -> int:
        return len(self._subscribers)

    def publish(self, rec: dict) -> None:
        """Hand a record to every live listener. Never raises: publishing
        is telemetry to whoever happens to be watching, never a stage the
        emitter can fail on."""
        for q, loop in list(self._subscribers.items()):
            try:
                loop.call_soon_threadsafe(self._offer, q, rec)
            except RuntimeError:
                self._subscribers.pop(q, None)  # its loop is gone

    @staticmethod
    def _offer(q: asyncio.Queue, rec: dict) -> None:
        if q.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                q.get_nowait()  # drop the oldest; a live UI wants the newest
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(rec)
