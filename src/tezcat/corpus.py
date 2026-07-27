"""Permanent labeled training corpus.

Every human judgment that flows through the harness — approve/deny on a
gated write, a git revert, a dashboard proposal decision — lands as one
self-contained JSONL row under data/corpus/YYYY-MM.jsonl. Rows inline
the full trajectory because chat transcripts are scratch (the vault is
the archive); anything that reconstructs via chat_id would be broken by
the time a fine-tune wants it. Capture only: no export or training
pipeline here. Four label sources: record_dashboard (proposal
decisions), record_confirm (the pane's write gate), record_revert
(the dashboard's ↺ and the CLI), record_reschedule (a human dragging
a task across the time axis).

Capture never breaks its caller: _append swallows its own failures, so a
full disk or an unserializable payload costs a training row, not a
decision, a gate, or a revert. The swallow lives HERE, once — spelled
per-call-site it is N chances to forget, and a bare `except OSError`
misses the TypeError json.dumps raises on an exotic payload.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from .bootstrap import ROOT

CORPUS_DIR = ROOT / "data" / "corpus"
SCHEMA_VERSION = 1


def _envelope(chat_id: str, model: str, messages: list[dict], label: dict) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "chat_id": chat_id,
        "model": model,
        "messages": messages,
        "label": label,
    }


def _append(rec: dict, corpus_dir: Path | None) -> Path | None:
    """Append one row to the current month's file. Returns None when
    nothing was written — either the row has nothing to train on (no
    trajectory and no self-contained payload: a proposal, or the
    sha+subject of a reverted commit), or writing it failed.

    Failure is swallowed HERE, once, for every caller. A label is
    telemetry: losing one must never cost the user the decision, the
    gate verdict, or the revert that produced it."""
    label = rec.get("label", {})
    if (not rec.get("messages") and not label.get("proposal")
            and not label.get("sha") and not label.get("task")):
        return None
    try:
        d = Path(corpus_dir) if corpus_dir else CORPUS_DIR
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{dt.datetime.now().strftime('%Y-%m')}.jsonl"
        # default=str so an exotic value in a trajectory (a date out of
        # YAML, a model object) degrades to its repr instead of raising
        # TypeError into a caller that only ever guarded against OSError.
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        return path
    except (OSError, TypeError, ValueError) as e:
        print(f"corpus row dropped ({type(e).__name__}: {e})", file=sys.stderr)
        return None


def record_confirm(
    chat_id: str,
    model: str,
    messages: list[dict],
    tool_name: str,
    tool_args: dict,
    approved: bool,
    timed_out: bool = False,
    corpus_dir: Path | None = None,
) -> Path | None:
    """A gated write the user approved or denied. `timed_out` marks the 600s
    silent-expiry deny so a future export can decide if it counts as a real
    negative."""
    label = {
        "source": "confirm",
        "polarity": "positive" if approved else "negative",
        "tool_name": tool_name,
        "tool_args": tool_args,
        "approved": approved,
        "timed_out": timed_out,
    }
    return _append(_envelope(chat_id, model, messages, label), corpus_dir)


def record_revert(
    chat_id: str,
    model: str,
    messages: list[dict],
    sha: str,
    commit_message: str,
    corpus_dir: Path | None = None,
) -> Path | None:
    """A harness commit the user reverted (dashboard ↺ or the CLI) — a
    self-contained negative: the sha + commit message identify the
    action being disavowed. Attributed via the commit's Chat-Id trailer
    when it carries one; transcripts are scratch, so `messages` is
    usually empty here."""
    label = {
        "source": "revert",
        "polarity": "negative",
        "sha": sha,
        "commit_message": commit_message,
    }
    return _append(_envelope(chat_id, model, messages, label), corpus_dir)


def record_reschedule(
    note: str,
    task_line: str,
    task_text: str,
    from_due: str | None,
    to_due: str | None,
    field: str = "due",
    corpus_dir: Path | None = None,
) -> Path | None:
    """A human moving a task along the time axis. `field` says which
    axis: "due" (📅, the rare hard commitment) or "scheduled" (⏳, the
    plan — where promote/defer live). No polarity — this is a scheduling
    label, not an approval: which tasks a person pulls forward and which
    they push is the signal a future triage worker trains on. The raw
    line makes the row self-contained."""
    if to_due and (not from_due or to_due < from_due):
        direction = "promote"
    elif from_due and (not to_due or to_due > from_due):
        direction = "defer"
    else:
        direction = "unchanged"
    label = {
        "source": "reschedule",
        "field": field,
        "note": note,
        "task": task_line,
        "task_text": task_text,
        "from_due": from_due,
        "to_due": to_due,
        "direction": direction,
    }
    return _append(_envelope("", "", [], label), corpus_dir)


def record_dashboard(
    proposal: dict,
    decision: str,
    corpus_dir: Path | None = None,
) -> Path | None:
    """A review-dashboard decision. Cleaner
    than chat labels: the row endorses one self-contained proposal, not a
    whole trajectory, so the proposal itself is the payload."""
    label = {
        "source": "dashboard",
        "polarity": "positive" if decision == "approved" else "negative",
        "decision": decision,
        "proposal": proposal,
    }
    return _append(_envelope(proposal.get("id", ""), "", [], label), corpus_dir)
