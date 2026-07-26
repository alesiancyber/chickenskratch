"""Harness/Today.md — the attention surface (v1: deterministic aggregator).

A dashboard is a VIEW, not a mutation: it proposes attention, never
changes, so it needs none of the trust machinery — no checkbox, no
verifier, no staleness. Wrongness costs a glance. v1 is pure tier-0
code over the existing parsers; a triage worker (ranking, nudges) is a
later, separately-evaled layer that must earn its place over this
deterministic core.

Design rule: ONE SCREEN, OR IT'S NOT A DASHBOARD — every section caps
its rows and folds the rest behind a count.

Split rule (owned-surfaces 0-C): `collect()` builds the typed view-model
and is the only code that reads vault state; `to_markdown()` and the UI
server are two renderers of that model. Neither renderer ever parses
the other's output.

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
from obsidian_mcp.vault import Vault, VaultError, read_lossy

SOON_DAYS = 3   # "due soon" horizon
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
    dated, boards = _walk(v, surface_dirs)
    return {
        "date": today.isoformat(),
        "pending_decisions": pending_decisions,
        "surfaces": {
            "review": cfg.surfaces.get("review", ""),
            "capture": cfg.surfaces.get("capture", ""),
            "changes": cfg.surfaces.get("changes", ""),
        },
        "due": _due_model(dated, today),
        "waiting": _waiting_rows(v, cfg.waiting_inbox, today),
        "stranded": _stranded_meeting_actions(v, cfg.meetings_dir, today),
        "meetings": _meetings_today(v, cfg.meetings_dir, today),
        "in_flight": _in_flight(boards),
        "activity": _activity_since_midnight(v, today, surface_dirs),
    }


def _due_model(dated: list[tuple[tk.Task, str]], today: dt.date) -> dict:
    """Bucketed so old debt can never crowd out the present. Overdue is
    ordered freshest-first (ancient overdue is the least actionable);
    soon is ordered soonest-first."""
    horizon = today + dt.timedelta(days=SOON_DAYS)
    out: dict = {"horizon": horizon.isoformat(),
                 "broken": [], "overdue": [], "due_today": [], "soon": []}
    for task, rel in dated:
        d = _safe_date(task.due)
        row = {"text": task.text, "due": task.due, "note": rel}
        if d is None:
            out["broken"].append(row)
        elif d < today:
            out["overdue"].append({**row, "days": (today - d).days})
        elif d == today:
            out["due_today"].append({**row, "days": 0})
        elif d <= horizon:
            out["soon"].append({**row, "days": (d - today).days})
    out["overdue"].sort(key=lambda r: r["days"])
    out["soon"].sort(key=lambda r: r["days"])
    return out


def _walk(v: Vault, surface_dirs: frozenset[str]):
    """One walk: (dated open tasks, boards). Kanban cards carry no dates
    and are reported via their boards, not the task list."""
    dated: list[tuple[tk.Task, str]] = []
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
            if not task.done and task.due:
                dated.append((task, rel))
    return dated, boards


def _waiting_rows(v: Vault, waiting_inbox: str, today: dt.date) -> list[dict]:
    try:
        text = v.read(waiting_inbox)
    except VaultError:
        return []
    rows = []
    for task in tk.iter_tasks(text):
        if task.done:
            continue
        born = _safe_date(task.created) if task.created else None
        rows.append({
            "text": task.text,
            "due": task.due or "",
            # ➕ birth dates stamp at format_task; aging accrues
            "waiting_days": (today - born).days if born else None,
            "note": waiting_inbox,
        })
    return rows


def _stranded_meeting_actions(v: Vault, meetings_dir: str, today: dt.date,
                              max_age_days: int = 7) -> list[dict]:
    """Open, UNDATED action items in old meeting notes — invisible to the
    Due section, rotting quietly. Dated ones already surface in Due."""
    try:
        notes = list(v.iter_notes(meetings_dir, recursive=False))
    except VaultError:
        return []
    rows = []
    for p in notes:
        try:
            met = dt.date.fromisoformat(p.stem[:10])
        except ValueError:
            continue
        if (today - met).days < max_age_days:
            continue
        for t in tk.iter_tasks(read_lossy(p)):
            if not t.done and not t.due:
                rows.append({"text": t.text, "age_days": (today - met).days, "note": v.rel(p)})
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
    due = f" — due {r['due']}" if r["due"] else ""
    days = r["waiting_days"]
    age = f" ({days}d waiting)" if days is not None and days >= WAITING_AGE_MIN else ""
    return f"- {r['text']}{due}{age} · {_link(r['note'])}"


def _in_flight_row_md(r: dict) -> str:
    cards = r["cards"]
    shown = ", ".join(cards[:5]) + (f", +{len(cards) - 5}" if len(cards) > 5 else "")
    return f"- {_link(r['note'])} · {r['column']}: {shown}"


def to_markdown(model: dict) -> str:
    """Pure function of the view-model — the degraded-mode renderer."""
    today = dt.date.fromisoformat(model["date"])
    review = model["surfaces"]["review"]
    capture = model["surfaces"]["capture"]
    changes = model["surfaces"]["changes"]

    lines = [
        f"# Today — {today.strftime('%A %Y-%m-%d')}",
        "",
        "_Machine-rendered view; edits here are overwritten._"
        + (f" _Decide in {_link(review)}," if review else "")
        + (f" dump into {_link(capture)}._" if capture else ""),
    ]

    if model["pending_decisions"] and review:
        lines += ["", f"⚠ **{model['pending_decisions']} pending decision(s) → {_link(review)}**"]

    lines += ["", "## Due", *_due_section_md(model["due"])]

    waiting = [_waiting_row_md(r) for r in model["waiting"]]
    if waiting:
        lines += ["", "## Waiting on", *_cap(waiting)]

    stranded = [f"- {r['text']} — {r['age_days']}d old · {_link(r['note'])}"
                for r in model["stranded"]]
    if stranded:
        lines += ["", "## Stranded in meetings", *_cap(stranded, 5)]

    meetings = [f"- {_link(rel)}" for rel in model["meetings"]]
    if meetings:
        lines += ["", "## Today's meetings", *_cap(meetings)]

    in_flight = [_in_flight_row_md(r) for r in model["in_flight"]]
    if in_flight:
        lines += ["", "## In flight", *_cap(in_flight)]

    if model["activity"] and changes:
        lines += ["", f"_{model['activity']} vault change(s) since midnight → {_link(changes)}_"]

    return "\n".join(lines) + "\n"


def render(v: Vault, cfg, pending_decisions: int, today: dt.date | None = None) -> str:
    return to_markdown(collect(v, cfg, pending_decisions, today))
