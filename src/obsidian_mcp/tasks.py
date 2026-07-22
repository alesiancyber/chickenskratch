"""Checkbox-task parsing using the Obsidian Tasks plugin conventions.

A task is a `- [ ]` / `- [x]` list line; metadata rides in emoji fields:
due date `📅 YYYY-MM-DD`, done date `✅ YYYY-MM-DD`, and priority
markers (🔺 highest, ⏫ high, 🔼 medium, 🔽 low, ⏬ lowest).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ids import split_block_ref

TASK_RE = re.compile(r"^(?P<indent>\s*)[-*] \[(?P<mark>.)\]\s+(?P<text>.*)$")
DUE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
DONE_RE = re.compile(r"✅\s*(\d{4}-\d{2}-\d{2})")

PRIORITY_MARKS = {"🔺": "highest", "⏫": "high", "🔼": "medium", "🔽": "low", "⏬": "lowest"}
PRIORITY_EMOJI = {v: k for k, v in PRIORITY_MARKS.items()}


@dataclass
class Task:
    line_no: int          # 0-based index into the note's lines
    raw: str
    text: str             # description without emoji metadata
    done: bool
    due: str | None
    priority: str | None
    done_date: str | None
    block_id: str | None = None   # trailing ^ref — the entity-id watermark

    def to_dict(self, path: str) -> dict:
        d = {"note": path, "line": self.line_no + 1, "text": self.text, "done": self.done}
        if self.due:
            d["due"] = self.due
        if self.priority:
            d["priority"] = self.priority
        if self.done_date:
            d["completed_on"] = self.done_date
        if self.block_id:
            d["id"] = self.block_id
        return d


def parse_task_line(line: str, line_no: int = 0) -> Task | None:
    m = TASK_RE.match(line)
    if not m:
        return None
    # Strip a trailing block ref first so text and metadata never see it.
    body, block_id = split_block_ref(m.group("text"))
    due = _first(DUE_RE, body)
    done_date = _first(DONE_RE, body)
    priority = next((name for mark, name in PRIORITY_MARKS.items() if mark in body), None)
    text = DUE_RE.sub("", body)
    text = DONE_RE.sub("", text)
    for mark in PRIORITY_MARKS:
        text = text.replace(mark, "")
    return Task(
        line_no=line_no,
        raw=line,
        text=text.strip(),
        done=m.group("mark").lower() == "x",
        due=due,
        priority=priority,
        done_date=done_date,
        block_id=block_id,
    )


def _first(pattern: re.Pattern, s: str) -> str | None:
    m = pattern.search(s)
    return m.group(1) if m else None


def format_task(
    text: str, due: str | None = None, priority: str | None = None, block_id: str | None = None
) -> str:
    parts = [f"- [ ] {text.strip()}"]
    if priority:
        emoji = PRIORITY_EMOJI.get(priority.lower())
        if emoji is None:
            raise ValueError(f"Unknown priority {priority!r}; use one of {sorted(PRIORITY_EMOJI)}")
        parts.append(emoji)
    if due:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
            raise ValueError(f"Due date must be YYYY-MM-DD, got {due!r}")
        parts.append(f"📅 {due}")
    if block_id:
        parts.append(f"^{block_id}")   # block refs must be the last token
    return " ".join(parts)


def iter_tasks(content: str):
    for i, line in enumerate(content.splitlines()):
        task = parse_task_line(line, i)
        if task:
            yield task
