"""Permanent labeled training corpus (DESIGN-training-corpus.md).

Every human judgment that flows through the harness — approve/deny on a
gated write, a bug report, a git revert, a dashboard proposal decision —
lands as one self-contained JSONL row under data/corpus/YYYY-MM.jsonl.
Rows inline the full trajectory because data/chats/ prunes after 3 days;
anything that reconstructs via chat_id would be broken by the time a
fine-tune wants it. Capture only: no export or training pipeline here.

Callers wrap every record_* call in `try/except OSError: pass` — corpus
capture must never break a live chat (same convention as stats.record_run).
"""

from __future__ import annotations

import datetime as dt
import json
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
    """Append one row to the current month's file. No-ops (None) when the
    row has nothing to train on: no trajectory and no proposal payload."""
    if not rec.get("messages") and not rec.get("label", {}).get("proposal"):
        return None
    d = Path(corpus_dir) if corpus_dir else CORPUS_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{dt.datetime.now().strftime('%Y-%m')}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


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


def record_bug_report(
    chat_id: str,
    model: str,
    messages: list[dict],
    description: str,
    report_path: str,
    corpus_dir: Path | None = None,
) -> Path | None:
    label = {
        "source": "bug_report",
        "polarity": "negative",
        "description": description,
        "report_path": report_path,
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
    """A harness commit the user reverted — attributed via its Chat-Id
    trailer while the chat is still within the 3-day retention window."""
    label = {
        "source": "revert",
        "polarity": "negative",
        "sha": sha,
        "commit_message": commit_message,
    }
    return _append(_envelope(chat_id, model, messages, label), corpus_dir)


def record_dashboard(
    proposal: dict,
    decision: str,
    corpus_dir: Path | None = None,
) -> Path | None:
    """A review-dashboard decision (DESIGN-review-dashboard.md §5). Cleaner
    than chat labels: the row endorses one self-contained proposal, not a
    whole trajectory, so the proposal itself is the payload."""
    label = {
        "source": "dashboard",
        "polarity": "positive" if decision == "approved" else "negative",
        "decision": decision,
        "proposal": proposal,
    }
    return _append(_envelope(proposal.get("id", ""), "", [], label), corpus_dir)
