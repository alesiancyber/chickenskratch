"""Pass events for the UI's SSE stream.

The engine is the only author; the UI server only tails (the daemon
stays the sole trigger authority — the UI listens and asks). One JSONL
file per vault under data/, append-only and size-rotated. Every write
is swallowed on OSError: event emission is telemetry, never a stage a
pass can fail on (I6). A dead or absent UI just means nobody reads the
file.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

from tezcat_vault.vault import atomic_write_text

from .bootstrap import ROOT, vault_digest
from .fanout import Fanout

MAX_BYTES = 512 * 1024
KEEP_LINES = 200
FANOUT_MAX = 200

# Live in-process listeners (the UI server's SSE streams). Almost every
# event this process serves originates in this process — a click's pass, a
# task checked off, a note saved — so the file tail is the wrong delivery
# path for them: it costs a poll interval of latency for something we
# already have in hand. The file stays the durable record and the ONLY
# path for other processes (tezcat-watch, the cron heartbeat), which is
# why this is an addition, not a replacement. The bounded per-listener
# fanout itself lives in fanout.py — the pane needs the same thing.
_live = Fanout(FANOUT_MAX)


def subscribe() -> asyncio.Queue:
    """Live feed for a stream running on the current loop."""
    return _live.subscribe()


def unsubscribe(q: asyncio.Queue) -> None:
    _live.unsubscribe(q)


def events_path(vault_path: str) -> Path:
    return ROOT / "data" / f"events-{vault_digest(vault_path)}.jsonl"


def emit(vault_path: str, event: str, payload: dict) -> None:
    rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
           "event": event, **payload}
    span = None
    try:
        path = events_path(vault_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        start = path.stat().st_size if path.exists() else 0
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        span = (start, start + len(line.encode("utf-8")))
        if path.stat().st_size > MAX_BYTES:
            # Rotation replaces the file atomically: a reader mid-tail
            # otherwise saw a truncated file, reset its offset to 0, and
            # replayed the last KEEP_LINES records to the browser — 200
            # duplicate refetches from a log trim. (An append from another
            # process can still be lost to the rotation itself; this file
            # is telemetry, and read_new tolerates the gap.)
            lines = path.read_text(encoding="utf-8").splitlines()[-KEEP_LINES:]
            atomic_write_text(path, "\n".join(lines) + "\n")
            span = None  # offsets just moved; let listeners re-sync by size
    except OSError:
        pass  # telemetry never fails a pass (I6) — still deliver it live
    # `_span` lets a live listener skip its own record when it later tails
    # the file, without racing another process's appends. Stripped before
    # the record reaches the browser.
    _live.publish({**rec, "_span": span} if span else rec)


def read_new(vault_path: str, offset: int) -> tuple[int, list[dict]]:
    """Records appended past byte `offset`, with the new offset.
    Tolerant by construction: an offset past EOF means the file rotated
    underneath us (restart at 0), a torn tail line waits for the next
    poll, and garbage lines are skipped — a corrupt event file degrades
    the stream, never the server."""
    path = events_path(vault_path)
    try:
        size = path.stat().st_size
    except OSError:
        return 0, []
    if offset > size:
        offset = 0
    if offset == size:
        return offset, []
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    end = chunk.rfind(b"\n")
    if end < 0:
        return offset, []
    records = []
    for ln in chunk[: end + 1].splitlines():
        try:
            records.append(json.loads(ln.decode("utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return offset + end + 1, records
