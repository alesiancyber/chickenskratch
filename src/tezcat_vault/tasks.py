"""Checkbox-task parsing using the Obsidian Tasks plugin conventions.

A task is a `- [ ]` / `- [x]` list line; metadata rides in emoji fields:
due date `📅 YYYY-MM-DD`, scheduled date `⏳ YYYY-MM-DD`, done date
`✅ YYYY-MM-DD`, created `➕ YYYY-MM-DD`, and priority markers
(🔺 highest, ⏫ high, 🔼 medium, 🔽 low, ⏬ lowest).

📅 and ⏳ mean different things and the distinction is load-bearing:
due is a hard external commitment (rare, never fabricated by the
harness); scheduled is a plan — "work on this then" — the judgment
field that promote/defer verbs move.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from .ids import attach_block_ref, split_block_ref

TASK_RE = re.compile(r"^(?P<indent>\s*)[-*] \[(?P<mark>.)\]\s+(?P<text>.*)$")
DUE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
SCHEDULED_RE = re.compile(r"[⏳⌛]\s*(\d{4}-\d{2}-\d{2})")  # both plugin glyphs parse; ⏳ is written
DONE_RE = re.compile(r"✅\s*(\d{4}-\d{2}-\d{2})")
CREATED_RE = re.compile(r"➕\s*(\d{4}-\d{2}-\d{2})")

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
    created: str | None = None    # ➕ birth date — stamped at format_task
    scheduled: str | None = None  # ⏳ plan date — the judgment field

    def to_dict(self, path: str) -> dict:
        d = {"note": path, "line": self.line_no + 1, "text": self.text, "done": self.done}
        if self.due:
            d["due"] = self.due
        if self.scheduled:
            d["scheduled"] = self.scheduled
        if self.priority:
            d["priority"] = self.priority
        if self.done_date:
            d["completed_on"] = self.done_date
        if self.created:
            d["created"] = self.created
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
    scheduled = _first(SCHEDULED_RE, body)
    done_date = _first(DONE_RE, body)
    created = _first(CREATED_RE, body)
    priority = next((name for mark, name in PRIORITY_MARKS.items() if mark in body), None)
    text = DUE_RE.sub("", body)
    text = SCHEDULED_RE.sub("", text)
    text = DONE_RE.sub("", text)
    text = CREATED_RE.sub("", text)
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
        created=created,
        scheduled=scheduled,
    )


def _first(pattern: re.Pattern, s: str) -> str | None:
    m = pattern.search(s)
    return m.group(1) if m else None


def _real_date(value: str, label: str) -> None:
    """Reject shapes AND impossible dates (2026-02-30 has the right shape
    but crashes every later fromisoformat): the digit regexes above only
    guarantee shape, so real-ness is checked here, at the write choke point."""
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label} date must be a real YYYY-MM-DD date, got {value!r}") from None


def format_task(
    text: str, due: str | None = None, priority: str | None = None,
    block_id: str | None = None, created: str | None = "auto",
    scheduled: str | None = None,
) -> str:
    """Render a task line. `created="auto"` (the default) stamps today's
    ➕ birth date — format_task is the choke point every harness-born
    task passes through, so lifecycle metadata (aging, nudges, hygiene)
    exists globally from day one instead of needing archaeology later.
    Pass created=None to skip. `scheduled` is the ⏳ plan field ("this
    week" captures stamp the week's Sunday); `due` stays the hard
    commitment."""
    parts = [f"- [ ] {text.strip()}"]
    if priority:
        emoji = PRIORITY_EMOJI.get(priority.lower())
        if emoji is None:
            raise ValueError(f"Unknown priority {priority!r}; use one of {sorted(PRIORITY_EMOJI)}")
        parts.append(emoji)
    if scheduled:
        _real_date(scheduled, "Scheduled")
        parts.append(f"⏳ {scheduled}")
    if due:
        _real_date(due, "Due")
        parts.append(f"📅 {due}")
    if created == "auto":
        created = dt.date.today().isoformat()
    if created:
        _real_date(created, "Created")
        parts.append(f"➕ {created}")
    if block_id:
        parts.append(f"^{block_id}")   # block refs must be the last token
    return " ".join(parts)


def _set_line_date(line: str, pattern: re.Pattern, glyph: str, label: str,
                   value: str | None) -> str:
    """Rewrite one emoji-date field of a task line in place; None removes
    it. Every other token — text, priority, the other dates, the trailing
    block ref — stays byte-identical, so a reschedule can never disturb
    the entity id or the lifecycle metadata."""
    if value:
        _real_date(value, label)
    core, ref = split_block_ref(line)
    if pattern.search(core):
        core = (pattern.sub(f"{glyph} {value}", core, count=1) if value
                else re.sub(r"\s*" + pattern.pattern, "", core).rstrip())
    elif value:
        # slot before the first later field, so lines keep format_task's
        # order (priority, ⏳ scheduled, 📅 due, ➕ created)
        later = [m for m in (CREATED_RE.search(core),
                             DUE_RE.search(core) if glyph == "⏳" else None) if m]
        at = min((m.start() for m in later), default=None)
        if at is not None:
            core = core[:at].rstrip() + f" {glyph} {value} " + core[at:]
        else:
            core = core.rstrip() + f" {glyph} {value}"
    return attach_block_ref(core, ref) if ref else core


def set_line_due(line: str, due: str | None) -> str:
    """Rewrite the 📅 due date — the hard-commitment field."""
    return _set_line_date(line, DUE_RE, "📅", "Due", due)


def set_line_scheduled(line: str, scheduled: str | None) -> str:
    """Rewrite the ⏳ scheduled date — the plan field the promote/defer
    verbs move. Never touches 📅: a plan is not a deadline."""
    return _set_line_date(line, SCHEDULED_RE, "⏳", "Scheduled", scheduled)


def iter_tasks(content: str):
    for i, line in enumerate(content.splitlines()):
        task = parse_task_line(line, i)
        if task:
            yield task
