"""Persistent per-model usage totals (data/stats.json).

Recorded from the interactive CLI after each completed run; evals are
deliberately excluded so benchmark traffic never pollutes real usage
numbers. Stats are cosmetic — a failure to record must never
break a chat, so callers wrap record_run in a try/except OSError.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from .bootstrap import ROOT

STATS_PATH = ROOT / "data" / "stats.json"
_COUNTERS = ("input_tokens", "output_tokens", "tool_calls")


def record_run(model: str, run: dict, path: Path | None = None) -> None:
    """Fold one run's counters into the model's totals."""
    path = path or STATS_PATH  # resolved at call time so STATS_PATH can be repointed
    path.parent.mkdir(parents=True, exist_ok=True)
    data = totals(path)
    m = data.setdefault(model, {"runs": 0, **{k: 0 for k in _COUNTERS}})
    m["runs"] += 1
    for key in _COUNTERS:
        m[key] += int(run.get(key) or 0)
    if run.get("estimated"):
        m["estimated"] = True  # some counts were chars/4 estimates, not server-reported
    m["last_used"] = dt.datetime.now().isoformat(timespec="seconds")
    # Temp + rename: concurrent recorders may still lose a fold (cosmetic),
    # but a torn write must never corrupt the file and zero every total.
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def totals(path: Path | None = None) -> dict:
    """All models' totals; a missing or corrupt file is just empty stats."""
    path = path or STATS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
