"""Checkbox-task parsing using the Obsidian Tasks plugin conventions.

A task is a `- [ ]` / `- [x]` list line; metadata rides in emoji fields:
due date `📅 YYYY-MM-DD`, scheduled date `⏳ YYYY-MM-DD`, done date
`✅ YYYY-MM-DD`, created `➕ YYYY-MM-DD`, owner `👤 Name`, and priority
markers (🔺 highest, ⏫ high, 🔼 medium, 🔽 low, ⏬ lowest).

📅 and ⏳ mean different things and the distinction is load-bearing:
due is a hard external commitment (rare, never fabricated by the
harness); scheduled is a plan — "work on this then" — the judgment
field that promote/defer verbs move.

👤 marks work someone ELSE owes the user ("👤 Nadia" — she owes this).
It replaces owed-ness-by-location (a task was owed because it sat in
Tasks/Waiting.md): once owed items live inside the records they came
from, the line itself has to say so. Not an Obsidian Tasks field — it
degrades to plain text anywhere else, which is fine.
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
# Owner is free text (a name, "the SOC"), so it runs to the next emoji
# field rather than a date shape. The block ref is split off before any
# field parsing, so ^ need not be excluded here.
OWNER_RE = re.compile(r"👤\s*([^📅⏳⌛✅➕🔺⏫🔼🔽⏬]+)")

PRIORITY_MARKS = {"🔺": "highest", "⏫": "high", "🔼": "medium", "🔽": "low", "⏬": "lowest"}
PRIORITY_EMOJI = {v: k for k, v in PRIORITY_MARKS.items()}


def week_end(d: dt.date) -> dt.date:
    """The Sunday ending d's week — THE ⏳ anchor for week-granular plans
    ("this week" stamps this Sunday, "next week" the one after). Defined
    once, in the grammar module, because three consumers need it — the
    dashboard's lanes (today.py), the extraction prompts (workers.py),
    the eval fixtures (evals.py) — and two of them disagreeing about
    which Sunday a week means is a real bug, not a style issue."""
    return d + dt.timedelta(days=6 - d.weekday())


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
    owner: str | None = None      # 👤 who owes this — owed work, not the user's

    def to_dict(self, path: str) -> dict:
        d = {"note": path, "line": self.line_no + 1, "text": self.text, "done": self.done}
        if self.due:
            d["due"] = self.due
        if self.scheduled:
            d["scheduled"] = self.scheduled
        if self.owner:
            d["owner"] = self.owner
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
    owner_m = OWNER_RE.search(body)
    owner = owner_m.group(1).strip() if owner_m else None
    priority = next((name for mark, name in PRIORITY_MARKS.items() if mark in body), None)
    # 👤 strips first: its free-text value runs to the next emoji field,
    # so it must be removed while those fields are still in place.
    text = OWNER_RE.sub("", body)
    text = DUE_RE.sub("", text)
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
        owner=owner,
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
    scheduled: str | None = None, owner: str | None = None,
) -> str:
    """Render a task line. `created="auto"` (the default) stamps today's
    ➕ birth date — format_task is the choke point every harness-born
    task passes through, so lifecycle metadata (aging, nudges, hygiene)
    exists globally from day one instead of needing archaeology later.
    Pass created=None to skip. `scheduled` is the ⏳ plan field ("this
    week" captures stamp the week's Sunday); `due` stays the hard
    commitment. `owner` marks owed work (👤, directly after the text —
    its free-text value parses up to the next emoji field, so it never
    sits between prose)."""
    parts = [f"- [ ] {text.strip()}"]
    if owner:
        parts.append(f"👤 {owner.strip()}")
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


def set_line_status(line: str, done: bool, today: str | None = None) -> str:
    """Flip a task's checkbox, keeping the ✅ stamp truthful — THE
    completion rewrite. The gated set_task_status tool and the editor's
    checkbox (/api/task/line) both ride this, so no surface can have its
    own idea of what "done" writes: completing stamps `today`'s ✅ (real
    today by default), reopening strips the stamp and its space — an
    open task must never carry a done date. Raises ValueError on a
    non-task line. The mark is rebuilt from the parsed position: string
    replacement would miss nonstandard marks like [/] and touch brackets
    in the task text."""
    m = TASK_RE.match(line)
    if m is None:
        raise ValueError(f"not a task line: {line!r}")
    mark_at = m.start("mark")
    line = line[:mark_at] + ("x" if done else " ") + line[mark_at + 1:]
    if done:
        if "✅" not in line:
            # A block ref must stay the last token, so ✅ slots in before it.
            core, ref = split_block_ref(line)
            line = f"{core} ✅ {today or dt.date.today().isoformat()}"
            if ref:
                line = attach_block_ref(line, ref)
    else:
        # Consume the space before ✅ too, or reopening leaves a double
        # space where the stamp sat.
        line = re.sub(r"\s*" + DONE_RE.pattern, "", line).rstrip()
    return line


def iter_tasks(content: str):
    for i, line in enumerate(content.splitlines()):
        task = parse_task_line(line, i)
        if task:
            yield task
