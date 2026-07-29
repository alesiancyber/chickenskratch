"""The attention view (deterministic aggregator).

A dashboard is a VIEW, not a mutation: it proposes attention, never
changes, so it needs none of the trust machinery — no verdict, no
verifier, no staleness. Wrongness costs a glance. `collect` feeds
/api/today; `to_markdown` is what `tezcat today` prints. v1 is pure
tier-0 code over the existing parsers; a triage worker (ranking,
nudges) is a later, separately-evaled layer that must earn its place
over this deterministic core.

Design rule: ONE SCREEN, OR IT'S NOT A DASHBOARD — every section caps
its rows and folds the rest behind a count.

Split rule: `collect()` builds the typed view-model and is the only
code that reads vault state; `to_markdown()` and the UI server are two
renderers of that model. Neither renderer ever parses the other's
output.

The date is the only clock input, so within a day the view is a pure
function of vault state; the activity count excludes Harness/-only
commits so the harness's own writes never inflate it. The 6am heartbeat
pass (config/launchd/com.tezcat.heartbeat.plist) covers the date
rollover on a quiet vault.
"""

from __future__ import annotations

import datetime as dt

from tezcat_vault import kanban as kb
from tezcat_vault import markdown as md
from tezcat_vault import tasks as tk
from tezcat_vault.vault import Vault, VaultError, read_lossy

SOON_DAYS = 3   # "due soon" horizon (the Today view's forward glance)


def week_end(d: dt.date) -> dt.date:
    """The Sunday ending d's week — the ⏳ anchor for week-granular plans
    ("this week" stamps this Sunday, "next week" the one after)."""
    return d + dt.timedelta(days=6 - d.weekday())
MAX_ROWS = 8    # per section; the rest folds behind a "+N more"
WAITING_AGE_MIN = 3  # days before a waiting item starts showing its age

# kanban columns that count as work in flight
_ACTIVE_COLUMNS = {"doing", "in progress", "active", "in-progress"}


def _link(rel: str) -> str:
    return f"[[{rel.removesuffix('.md')}]]"


def _cap(rows: list[str], n: int = MAX_ROWS, label: str = "") -> list[str]:
    if len(rows) <= n:
        return rows
    tail = f"- … +{len(rows) - n} more {label}".rstrip()
    return rows[:n] + [tail]


def _safe_date(value: str) -> dt.date | None:
    """A hand-typed 📅 2026-02-30 has the right shape but is not a date;
    the dashboard must report it, never crash the pass over it."""
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


# ---- the view-model ----


def collect(v: Vault, cfg, pending_decisions: int, today: dt.date | None = None) -> dict:
    """The attention view-model: every section as typed rows, JSON-ready.
    Row caps and phrasing are the renderers' business; the model carries
    the full sets."""
    today = today or dt.date.today()
    surface_dirs = cfg.surface_dirs
    open_tasks, owed_tasks, boards = _walk(v, surface_dirs)
    due, planned, this_week, next_week, later = _time_model(open_tasks, today)
    return {
        "date": today.isoformat(),
        "pending_decisions": pending_decisions,
        # Capture is the only vault surface there is to point at.
        "surfaces": {"capture": cfg.surfaces.get("capture", "")},
        "due": due,
        # The week-granular lanes: planned = ⏳ today or carried; this
        # week / next week are calendar weeks (Sunday-anchored) mixing
        # hard dues and plans, each row labeled; later = dated past next
        # week; backlog = the long-term queue (no date, no week) that
        # promotion and plan_today draw from.
        "planned": planned,
        "this_week": this_week,
        "next_week": next_week,
        "later": later,
        "backlog": _backlog(open_tasks, today),
        "waiting": _waiting_rows(owed_tasks, today),
        "stranded": _stranded(open_tasks, today),
        "meetings": _meetings_today(v, cfg.meetings_dir, today),
        "in_flight": _in_flight(boards),
        "activity": _activity_since_midnight(v, today, surface_dirs),
    }


def _time_model(open_tasks: list[tuple[tk.Task, str]], today: dt.date):
    """(due buckets, planned, this_week, next_week, later). Bucketed so
    old debt can never crowd out the present: overdue is ordered
    freshest-first (ancient overdue is the least actionable).

    Precedence per task — one task, one row:
    - a hard 📅 at or past today outranks any plan (a deadline is a fact)
    - ⏳ at or past today is TODAY's row (`slipped` counts the carry —
      a week plan that ends undone rolls into today for re-deciding)
    - 📅 inside the soon horizon still surfaces on Today (imminent
      deadlines outrank week filing)
    - otherwise the earliest of ⏳/📅 files the task into its calendar
      week: this week, next week, or later — `kind` says which field
      placed it, so the row can say "due Thu" vs "planned".
"""
    horizon = today + dt.timedelta(days=SOON_DAYS)
    this_sunday = week_end(today)
    next_sunday = this_sunday + dt.timedelta(days=7)
    due: dict = {"horizon": horizon.isoformat(),
                 "this_sunday": this_sunday.isoformat(),
                 "next_sunday": next_sunday.isoformat(),
                 "broken": [], "overdue": [], "due_today": [], "soon": []}
    planned: list[dict] = []
    this_week: list[dict] = []
    next_week: list[dict] = []
    later: list[dict] = []
    for task, rel in open_tasks:
        d = _safe_date(task.due) if task.due else None
        s = _safe_date(task.scheduled) if task.scheduled else None
        row = {"text": task.text, "due": task.due or "",
               "scheduled": task.scheduled or "", "note": rel}
        if task.due and d is None:
            due["broken"].append(row)
        elif d is not None and d < today:
            due["overdue"].append({**row, "days": (today - d).days})
        elif d is not None and d == today:
            due["due_today"].append({**row, "days": 0})
        elif s is not None and s <= today:
            planned.append({**row, "slipped": (today - s).days})
        else:
            # soon (Today's forward glance) and the week buckets (the
            # Week tab) are different VIEWS: a task due in 2 days
            # legitimately rows in both.
            if d is not None and d <= horizon:
                due["soon"].append({**row, "days": (d - today).days})
            when = min(x for x in (d, s) if x is not None) if (d or s) else None
            if when is None:
                continue  # undated & unplanned: the backlog's business
            week_row = {**row, "when": when.isoformat(),
                        "kind": "due" if when == d else "planned",
                        "days": (when - today).days}
            if when <= this_sunday:
                this_week.append(week_row)
            elif when <= next_sunday:
                next_week.append(week_row)
            else:
                later.append(week_row)
    due["overdue"].sort(key=lambda r: r["days"])
    due["soon"].sort(key=lambda r: r["days"])
    planned.sort(key=lambda r: -r["slipped"])       # longest-carried first
    for bucket in (this_week, next_week, later):
        bucket.sort(key=lambda r: (r["kind"] != "due", r["days"]))
    return due, planned, this_week, next_week, later[:50]


def _backlog(open_tasks: list[tuple[tk.Task, str]],
             today: dt.date, cap: int = 100) -> list[dict]:
    """The judgment pool: every open, top-level task with neither a 📅
    nor a ⏳ — nothing the time model can place, wherever it lives (a
    record's action items are as plannable as anything else). Oldest
    first (age is the strongest deterministic surfacing signal we
    have), bounded so a neglected vault costs one big list, not an
    unbounded one."""
    rows = []
    for task, rel in open_tasks:
        if task.due or task.scheduled:
            continue
        if task.raw.lstrip() != task.raw:
            continue  # subtasks ride with their parent
        born = _safe_date(task.created) if task.created else None
        rows.append({"text": task.text, "note": rel,
                     "priority": task.priority or "",
                     "age_days": (today - born).days if born else None,
                     "id": task.block_id or ""})
    rows.sort(key=lambda r: -(r["age_days"] or -1))
    return rows[:cap]


def _walk(v: Vault, surface_dirs: frozenset[str]):
    """One walk: (the user's open tasks, open owed items, boards),
    partitioned by 👤 HERE — the one choke point — so no view downstream
    can mistake somebody else's debt for the user's work (a snoozed
    "👤 Nadia ⏳ next-week" must never surface as planned work). Kanban
    cards carry no dates and are reported via their boards, not the
    task list."""
    open_tasks: list[tuple[tk.Task, str]] = []
    owed_tasks: list[tuple[tk.Task, str]] = []
    boards: list[tuple[str, kb.Board]] = []
    for p, rel in v.iter_knowledge_notes(surface_dirs):
        text = read_lossy(p)
        fm, _body = md.split_frontmatter(text)
        if "kanban-plugin" in fm:
            try:
                boards.append((rel, kb.parse_board(text)))
            except Exception:
                pass  # a malformed board never breaks the dashboard
            continue
        for task in tk.iter_tasks(text):
            if not task.done:
                (owed_tasks if task.owner else open_tasks).append((task, rel))
    return open_tasks, owed_tasks, boards


def _waiting_rows(owed_tasks: list[tuple[tk.Task, str]],
                  today: dt.date) -> list[dict]:
    """The waiting view: a filter over the walk — owed-ness is the 👤
    field, and the row links to the record the commitment was made in.
    An owed item whose ⏳ is in the future is snoozed: "chase this then"
    is the same field as "work on this then", no new mechanism.
    Longest-waiting first."""
    rows = []
    for task, rel in owed_tasks:
        s = _safe_date(task.scheduled) if task.scheduled else None
        if s is not None and s > today:
            continue  # snoozed until its ⏳ arrives
        born = _safe_date(task.created) if task.created else None
        rows.append({
            "text": task.text,
            "owner": task.owner or "",
            "due": task.due or "",
            # ➕ birth dates stamp at format_task; aging accrues
            "waiting_days": (today - born).days if born else None,
            "note": rel,
        })
    rows.sort(key=lambda r: -(r["waiting_days"] if r["waiting_days"] is not None else -1))
    return rows


def _filename_date(rel: str) -> dt.date | None:
    """The YYYY-MM-DD a note's filename opens with, if any — the fallback
    age clock for tasks born before ➕ stamping (old meeting notes)."""
    try:
        return dt.date.fromisoformat(rel.rsplit("/", 1)[-1][:10])
    except ValueError:
        return None


def _stranded(open_tasks: list[tuple[tk.Task, str]], today: dt.date,
              max_age_days: int = 7) -> list[dict]:
    """The attention engine, v1: open, undated, unplanned tasks older
    than N days, WHEREVER they live — invisible to Due and Planned,
    rotting quietly. Was a meetings-only feature aging by filename; now
    every record's tasks age by their ➕ created stamp (filename date as
    the fallback for pre-stamp lines). A task with neither clock is the
    backlog's business — it can't be aged, only listed.

    Oldest first, stated rather than inherited: the section only folds
    the first few rows into view, so the order IS which items you get
    told about."""
    rows = []
    for task, rel in open_tasks:
        if task.due or task.scheduled:
            continue
        born = (_safe_date(task.created) if task.created else None) or _filename_date(rel)
        if born is None or (today - born).days < max_age_days:
            continue
        rows.append({"text": task.text, "age_days": (today - born).days, "note": rel})
    rows.sort(key=lambda r: -r["age_days"])
    return rows


def _meetings_today(v: Vault, meetings_dir: str, today: dt.date) -> list[str]:
    try:
        notes = list(v.iter_notes(meetings_dir, recursive=False))
    except VaultError:
        return []
    prefix = today.isoformat()
    return [v.rel(p) for p in notes if p.stem.startswith(prefix)]


def _in_flight(boards: list[tuple[str, kb.Board]]) -> list[dict]:
    rows = []
    for rel, board in boards:
        for col in board.columns:
            if col.name.lower() not in _ACTIVE_COLUMNS:
                continue
            cards = [c["text"] for c in col.cards if not c["done"]]
            if cards:
                rows.append({"note": rel, "column": col.name, "cards": cards})
    return rows


def _activity_since_midnight(v: Vault, today: dt.date, surface_dirs: frozenset[str]) -> int:
    """Snapshot commits since midnight, excluding those that touched only
    Harness/ surfaces — the harness maintaining its own surfaces is not
    the user's activity."""
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


# ---- the markdown renderer ----


def _due_row_md(row: dict, bucket: str) -> str:
    if bucket == "overdue":
        when = f"⚠ **{row['days']}d overdue**"
    elif bucket == "due_today":
        when = "**due today**"
    else:
        when = f"due in {row['days']}d"
    return f"- {row['text']} — {when} · {_link(row['note'])}"


def _due_section_md(due: dict) -> list[str]:
    rows = [f"- ⚠ {r['text']} — due date `{r['due']}` is not a real date · {_link(r['note'])}"
            for r in due["broken"]]
    rows += _cap([_due_row_md(r, "overdue") for r in due["overdue"]], 5, "overdue")
    rows += [_due_row_md(r, "due_today") for r in due["due_today"]]
    rows += _cap([_due_row_md(r, "soon") for r in due["soon"]], 5, "coming up")
    if not rows:
        horizon = dt.date.fromisoformat(due["horizon"])
        rows = [f"- nothing due through {horizon.strftime('%A %Y-%m-%d')}"]
    return rows


def _waiting_row_md(r: dict) -> str:
    who = f"{r['owner']}: " if r.get("owner") else ""
    due = f" — due {r['due']}" if r["due"] else ""
    days = r["waiting_days"]
    age = f" ({days}d waiting)" if days is not None and days >= WAITING_AGE_MIN else ""
    return f"- {who}{r['text']}{due}{age} · {_link(r['note'])}"


def _in_flight_row_md(r: dict) -> str:
    cards = r["cards"]
    shown = ", ".join(cards[:5]) + (f", +{len(cards) - 5}" if len(cards) > 5 else "")
    return f"- {_link(r['note'])} · {r['column']}: {shown}"


def to_markdown(model: dict) -> str:
    """Pure function of the view-model: what `tezcat today` prints, the
    headless view for when the dashboard isn't running. Never written
    into the vault."""
    today = dt.date.fromisoformat(model["date"])
    capture = model["surfaces"]["capture"]

    lines = [
        f"# Today — {today.strftime('%A %Y-%m-%d')}",
        "",
        "_A view, printed on demand — nothing here is a file to edit._"
        + (f" _Dump into {_link(capture)}._" if capture else ""),
    ]

    if model["pending_decisions"]:
        lines += ["", f"⚠ **{model['pending_decisions']} pending decision(s) — "
                      f"the dashboard, or `tezcat decide <id> approve`**"]

    lines += ["", "## Due", *_due_section_md(model["due"])]

    planned = [f"- {r['text']}"
               + (f" — carried {r['slipped']}d" if r["slipped"] else "")
               + f" · {_link(r['note'])}" for r in model.get("planned", [])]
    if planned:
        lines += ["", "## Planned today", *_cap(planned)]

    waiting = [_waiting_row_md(r) for r in model["waiting"]]
    if waiting:
        lines += ["", "## Waiting on", *_cap(waiting)]

    stranded = [f"- {r['text']} — {r['age_days']}d old · {_link(r['note'])}"
                for r in model["stranded"]]
    if stranded:
        lines += ["", "## Stranded — undated, aging", *_cap(stranded, 5)]

    meetings = [f"- {_link(rel)}" for rel in model["meetings"]]
    if meetings:
        lines += ["", "## Today's meetings", *_cap(meetings)]

    in_flight = [_in_flight_row_md(r) for r in model["in_flight"]]
    if in_flight:
        lines += ["", "## In flight", *_cap(in_flight)]

    if model.get("backlog"):
        lines += ["", f"_{len(model['backlog'])} undated task(s) in the backlog "
                      f"— plan from the dashboard's Week tab_"]

    if model["activity"]:
        lines += ["", f"_{model['activity']} vault change(s) since midnight "
                      f"— `tezcat changes`_"]

    return "\n".join(lines) + "\n"


def render(v: Vault, cfg, pending_decisions: int, today: dt.date | None = None) -> str:
    return to_markdown(collect(v, cfg, pending_decisions, today))
