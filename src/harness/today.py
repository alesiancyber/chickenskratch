"""Harness/Today.md — the attention surface (v1: deterministic aggregator).

A dashboard is a VIEW, not a mutation: it proposes attention, never
changes, so it needs none of the trust machinery — no checkbox, no
verifier, no staleness. Wrongness costs a glance. v1 is pure tier-0
code over the existing parsers; a triage worker (ranking, nudges) is a
later, separately-evaled layer that must earn its place over this
deterministic core.

Design rule: ONE SCREEN, OR IT'S NOT A DASHBOARD — every section caps
its rows and folds the rest behind a count.

Churn discipline: the date is the only clock input, so within a day the
render is a pure function of vault state — written only on change, and
the activity count excludes Harness/-only commits so a render can never
feed the next one. The 6am heartbeat pass (config/launchd/
com.tezcat.heartbeat.plist) guarantees the date rollover renders even
when the vault is quiet.
"""

from __future__ import annotations

import datetime as dt

from obsidian_mcp import kanban as kb
from obsidian_mcp import markdown as md
from obsidian_mcp import tasks as tk
from obsidian_mcp.vault import Vault, VaultError

SOON_DAYS = 3   # "due soon" horizon
MAX_ROWS = 8    # per section; the rest folds behind a "+N more"

# kanban columns that count as work in flight
_ACTIVE_COLUMNS = {"doing", "in progress", "active", "in-progress"}


def _link(rel: str) -> str:
    return f"[[{rel.removesuffix('.md')}]]"


def _cap(rows: list[str], n: int = MAX_ROWS, label: str = "") -> list[str]:
    if len(rows) <= n:
        return rows
    tail = f"- … +{len(rows) - n} more {label}".rstrip()
    return rows[:n] + [tail]


def _due_row(task: tk.Task, rel: str, today: dt.date) -> str:
    due = dt.date.fromisoformat(task.due)
    if due < today:
        when = f"⚠ **{(today - due).days}d overdue**"
    elif due == today:
        when = "**due today**"
    else:
        when = f"due in {(due - today).days}d"
    return f"- {task.text} — {when} · {_link(rel)}"


def _due_section(dated: list[tuple[tk.Task, str]], today: dt.date) -> list[str]:
    """Bucketed so old debt can never crowd out the present: due-today
    always shows in full; overdue is capped, freshest debt first (ancient
    overdue is the least actionable); due-soon is capped."""
    horizon = today + dt.timedelta(days=SOON_DAYS)
    overdue, due_today, soon = [], [], []
    for task, rel in dated:
        d = dt.date.fromisoformat(task.due)
        if d < today:
            overdue.append((d, task, rel))
        elif d == today:
            due_today.append((d, task, rel))
        elif d <= horizon:
            soon.append((d, task, rel))
    rows = _cap([_due_row(t, r, today) for _d, t, r in
                 sorted(overdue, key=lambda x: x[0], reverse=True)], 5, "overdue")
    rows += [_due_row(t, r, today) for _d, t, r in due_today]
    rows += _cap([_due_row(t, r, today) for _d, t, r in sorted(soon, key=lambda x: x[0])],
                 5, "coming up")
    if not rows:
        rows = [f"- nothing due through {horizon.strftime('%A %Y-%m-%d')}"]
    return rows


def _collect(v: Vault, surface_dirs: frozenset[str]):
    """One walk: (dated open tasks, boards). Kanban cards carry no dates
    and are reported via their boards, not the task list."""
    dated: list[tuple[tk.Task, str]] = []
    boards: list[tuple[str, kb.Board]] = []
    for p in v.iter_notes():
        rel = v.rel(p)
        if rel.split("/")[0] in surface_dirs:
            continue
        text = p.read_text(encoding="utf-8")
        fm, _body = md.split_frontmatter(text)
        if "kanban-plugin" in fm:
            try:
                boards.append((rel, kb.parse_board(text)))
            except Exception:
                pass  # a malformed board never breaks the dashboard
            continue
        for task in tk.iter_tasks(text):
            if not task.done and task.due:
                dated.append((task, rel))
    return dated, boards


def _waiting_rows(v: Vault, waiting_inbox: str) -> list[str]:
    try:
        text = v.read(waiting_inbox)
    except VaultError:
        return []
    rows = []
    for task in tk.iter_tasks(text):
        if task.done:
            continue
        due = f" — due {task.due}" if task.due else ""
        rows.append(f"- {task.text}{due} · {_link(waiting_inbox)}")
    return rows


def _meetings_today(v: Vault, today: dt.date) -> list[str]:
    try:
        notes = list(v.iter_notes("Meetings", recursive=False))
    except VaultError:
        return []
    prefix = today.isoformat()
    return [f"- {_link(v.rel(p))}" for p in notes if p.stem.startswith(prefix)]


def _in_flight(boards: list[tuple[str, kb.Board]]) -> list[str]:
    rows = []
    for rel, board in boards:
        for col in board.columns:
            if col.name.lower() not in _ACTIVE_COLUMNS:
                continue
            cards = [c["text"] for c in col.cards if not c["done"]]
            if cards:
                shown = ", ".join(cards[:5]) + (f", +{len(cards) - 5}" if len(cards) > 5 else "")
                rows.append(f"- {_link(rel)} · {col.name}: {shown}")
    return rows


def _activity_since_midnight(v: Vault, today: dt.date, surface_dirs: frozenset[str]) -> int:
    """Snapshot commits since midnight, excluding those that touched only
    Harness/ surfaces — so this render's own commit (and Review/Changes
    re-renders) can never change the count and feed the next render."""
    v.ensure_repo()
    r = v._git("log", "--since", f"{today.isoformat()}T00:00:00",
               "--pretty=format:%x1e%H", "--name-only")
    if r.returncode != 0:
        return 0
    count = 0
    for record in r.stdout.split("\x1e"):
        files = [ln for ln in record.splitlines()[1:] if ln.strip()]
        if files and not all(f.split("/")[0] in surface_dirs for f in files):
            count += 1
    return count


def render(v: Vault, cfg, pending_decisions: int, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    surface_dirs = cfg.surface_dirs
    dated, boards = _collect(v, surface_dirs)

    review = cfg.surfaces.get("review", "")
    capture = cfg.surfaces.get("capture", "")
    changes = cfg.surfaces.get("changes", "")

    lines = [
        f"# Today — {today.strftime('%A %Y-%m-%d')}",
        "",
        "_Machine-rendered view; edits here are overwritten._"
        + (f" _Decide in {_link(review)}," if review else "")
        + (f" dump into {_link(capture)}._" if capture else ""),
    ]

    if pending_decisions and review:
        lines += ["", f"⚠ **{pending_decisions} pending decision(s) → {_link(review)}**"]

    lines += ["", "## Due", *_due_section(dated, today)]

    waiting = _waiting_rows(v, cfg.waiting_inbox)
    if waiting:
        lines += ["", "## Waiting on", *_cap(waiting)]

    meetings = _meetings_today(v, today)
    if meetings:
        lines += ["", "## Today's meetings", *_cap(meetings)]

    in_flight = _in_flight(boards)
    if in_flight:
        lines += ["", "## In flight", *_cap(in_flight)]

    activity = _activity_since_midnight(v, today, surface_dirs)
    if activity and changes:
        lines += ["", f"_{activity} vault change(s) since midnight → {_link(changes)}_"]

    return "\n".join(lines) + "\n"
