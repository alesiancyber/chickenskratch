"""Structured diff view-models for proposal previews.

The opcode walk, word-level marks, and long-run collapsing, emitted as
JSON-ready rows the UI server ships and the dashboard renders under its
own design tokens. This module emits data, never HTML — recycle
contracts, state, and logic, never renderings. The plain-text fallback
is the classic unified ```diff (applier.preview).
"""

from __future__ import annotations

import difflib
import re

_TOKEN_RE = re.compile(r"\S+|\s+")

CONTEXT = 3  # visible lines on each side of a change; longer equal runs collapse

# Past this many lines a note loses its preview instead of costing CPU:
# SequenceMatcher is quadratic, and no diff is worth hanging a pass (or a
# request) over. Lives here, with the diff machinery, so every renderer
# shares the one ceiling instead of hard-coding its own.
MAX_PREVIEW_LINES = 4000


def _mark_words(old: str, new: str) -> tuple[list[dict], list[dict]]:
    """Word-level segments for a paired changed line, GitHub-style:
    [{"text", "mark"}] per side, marked where the words differ."""
    a, b = _TOKEN_RE.findall(old), _TOKEN_RE.findall(new)
    left, right = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        old_part, new_part = "".join(a[i1:i2]), "".join(b[j1:j2])
        if old_part:
            left.append({"text": old_part, "mark": tag != "equal"})
        if new_part:
            right.append({"text": new_part, "mark": tag != "equal"})
    return left, right


def diff_rows(before: str, after: str) -> list[dict]:
    """The structured diff between two versions of a note. Row kinds:
    {"kind": "skip", "count": n}                       — collapsed equal run
    {"kind": "context", "ln", "rn", "text"}            — shared line
    {"kind": "change", "ln", "rn", "left", "right"}    — ln/rn None on the
        absent side; left/right are word segments ({"text", "mark"}) or
        None on the absent side.
    """
    if before == after:
        return []
    a, b = before.splitlines(), after.splitlines()
    opcodes = difflib.SequenceMatcher(None, a, b).get_opcodes()
    rows: list[dict] = []
    for k, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            # context lines hug the changes; long unchanged runs collapse
            run = i2 - i1
            head = CONTEXT if k > 0 else 0
            tail = CONTEXT if k < len(opcodes) - 1 else 0
            if run <= head + tail + 1:
                show = set(range(run))
            else:
                show = set(range(head)) | set(range(run - tail, run))
            skip_emitted = False
            for off in range(run):
                if off in show:
                    rows.append({"kind": "context", "ln": i1 + off + 1,
                                 "rn": j1 + off + 1, "text": a[i1 + off]})
                elif not skip_emitted:
                    rows.append({"kind": "skip", "count": run - len(show)})
                    skip_emitted = True
        elif tag == "replace":
            n = max(i2 - i1, j2 - j1)
            for off in range(n):
                has_l, has_r = i1 + off < i2, j1 + off < j2
                if has_l and has_r:
                    left, right = _mark_words(a[i1 + off], b[j1 + off])
                else:
                    left = [{"text": a[i1 + off], "mark": False}] if has_l else None
                    right = [{"text": b[j1 + off], "mark": False}] if has_r else None
                rows.append({"kind": "change",
                             "ln": i1 + off + 1 if has_l else None,
                             "rn": j1 + off + 1 if has_r else None,
                             "left": left, "right": right})
        elif tag == "delete":
            for off in range(i2 - i1):
                rows.append({"kind": "change", "ln": i1 + off + 1, "rn": None,
                             "left": [{"text": a[i1 + off], "mark": False}], "right": None})
        elif tag == "insert":
            for off in range(j2 - j1):
                rows.append({"kind": "change", "ln": None, "rn": j1 + off + 1,
                             "left": None, "right": [{"text": b[j1 + off], "mark": False}]})
    return rows
