"""Obsidian vault MCP server.

Exposes semantic tools (tasks, kanban, meeting notes) alongside general
note tools (search, read, section edits). Any MCP client can use this
server; the harness in this repo is just one of them. Every mutation is
bracketed by git snapshots so changes are always revertible.

Run: OBSIDIAN_VAULT_PATH=/path/to/vault obsidian-mcp
"""

from __future__ import annotations

import datetime as dt

import yaml
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import ids
from . import kanban as kb
from . import markdown as md
from . import schemas as sc
from . import tasks as tk
from . import vaultconfig as vc
from . import workers as wk
from .vault import VaultError, get_vault

READ_ONLY = ToolAnnotations(readOnlyHint=True)

mcp = FastMCP("obsidian-vault")

# Daily-note folder comes from the vault spec (`daily_dir`), so Foam's
# dailyNotes.directory and this harness can agree by config, not code.
MEETINGS_DIR = "Meetings"


def _today() -> str:
    return dt.date.today().isoformat()


def _valid_date(value: str, field: str = "date") -> str:
    """Reject non-ISO dates before they become filenames like Daily/tomorrow.md."""
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        raise VaultError(
            f"{field} must be a real date in YYYY-MM-DD form, got {value!r}. "
            f"(Today is {_today()}; resolve words like 'tomorrow' to a date first.)"
        ) from None


# --------------- reading & searching ---------------

@mcp.tool(annotations=READ_ONLY)
def list_notes(folder: str = "", recursive: bool = True) -> dict:
    """List markdown notes in the vault (or one folder), as vault-relative paths.

    Example: list_notes(folder="Projects") -> {"notes": ["Projects/Website Redesign.md", ...]}
    """
    v = get_vault()
    return {"notes": [v.rel(p) for p in v.iter_notes(folder, recursive)]}


@mcp.tool(annotations=READ_ONLY)
def read_note(path: str, section: str = "") -> dict:
    """Read a note. Pass `section` (a heading, e.g. "Decisions") to get just that
    section of a long note. Returns frontmatter, content, and the note's headings.

    Example: read_note(path="Meetings/2026-07-15 Sprint Planning", section="Action items")
    """
    v = get_vault()
    text = v.read(path)
    fm, body = md.split_frontmatter(text)
    if section:
        loc = md.find_section(body, section)
        if loc is None:
            raise VaultError(f"Heading {section!r} not found. Headings: {md.list_headings(body)}")
        start, end, _ = loc
        body = "\n".join(body.splitlines()[start:end])
    return {"path": path, "frontmatter": fm, "headings": md.list_headings(body), "content": body}


@mcp.tool(annotations=READ_ONLY)
def search_vault(query: str, folder: str = "", limit: int = 20, offset: int = 0) -> dict:
    """Search all notes for a case-insensitive substring; also matches note titles.
    Returns matching lines with note path and line number. Use offset to page.

    Example: search_vault(query="quarterly review") ->
      {"matches": [{"note": "Daily/2026-07-16.md", "line": 12, "text": "..."}], "total": 3}
    """
    v = get_vault()
    q = query.lower()
    matches = []
    for p in v.iter_notes(folder):
        rel = v.rel(p)
        if q in p.stem.lower():
            matches.append({"note": rel, "line": 0, "text": f"(title match: {p.stem})"})
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if q in line.lower():
                matches.append({"note": rel, "line": i, "text": line.strip()[:200]})
    page = matches[offset : offset + limit]
    result = {"matches": page, "total": len(matches)}
    if offset + limit < len(matches):
        result["next_offset"] = offset + limit
    return result


# --------------- writing notes ---------------

@mcp.tool()
def create_note(path: str, content: str = "") -> dict:
    """Create a new note. Fails if the note already exists (use append_to_note or
    replace_section to change existing notes). Folders are created as needed.

    Example: create_note(path="Tasks/Vector DBs.md", content="# Vector DBs\\n\\n- ...")
    """
    v = get_vault()
    p = v.resolve(path)
    with v.locked():
        # Existence is checked under the lock: two concurrent creators must
        # collide here, not silently overwrite each other.
        if p.exists():
            raise VaultError(f"Note already exists: {v.rel(p)}")
        v.snapshot_before("create_note")
        v.write(v.rel(p), content if content.endswith("\n") else content + "\n")
        v.snapshot_after(f"create note {v.rel(p)}")
    return {"created": v.rel(p)}


@mcp.tool()
def append_to_note(path: str, content: str, section: str = "") -> dict:
    """Append markdown to the end of a note, or to the end of one section if
    `section` (a heading) is given. Creates the note if it doesn't exist.

    Example: append_to_note(path="Daily/2026-07-17", section="Notes", content="- Called the vendor")
    """
    v = get_vault()
    p = v.resolve(path)  # validate before snapshotting
    with v.locked():
        v.snapshot_before("append_to_note")
        if not p.exists():
            v.write(v.rel(p), content.strip("\n") + "\n")
            v.snapshot_after(f"create note {v.rel(p)}")
            return {"created": v.rel(p)}
        text = v.read(path)
        fm, body = md.split_frontmatter(text)
        if section:
            body = md.append_to_section(body, section, content)
        else:
            body = body.rstrip("\n") + "\n\n" + content.strip("\n") + "\n"
        v.write(v.rel(p), md.join_frontmatter(fm, body))
        v.snapshot_after(f"append to {v.rel(p)}")
    return {"updated": v.rel(p)}


@mcp.tool()
def replace_section(path: str, heading: str, content: str) -> dict:
    """Replace everything under one heading of a note (the heading itself stays).
    The section's previous content is discarded entirely — do not include it in
    `content` unless the user asked to keep it. This is the safe way to rewrite
    part of a note without touching the rest.

    Example: replace_section(path="Projects/Website Redesign", heading="Status",
                             content="On track. Launch moved to Aug 3.")
    """
    v = get_vault()
    with v.locked():
        text = v.read(path)
        fm, body = md.split_frontmatter(text)
        v.snapshot_before("replace_section")
        body = md.replace_section_text(body, heading, content)
        v.write(path, md.join_frontmatter(fm, body))
        v.snapshot_after(f"replace section {heading!r} in {path}")
    return {"updated": path, "section": heading}


@mcp.tool()
def replace_line(path: str, line_text: str, content: str) -> dict:
    """Replace ONE exact line of a note with new content (which may be
    several lines). line_text must match a whole line exactly; the call
    fails if the line is absent or appears more than once. This is the
    precision tool for in-place rewrites — inline //commands ride on it.

    Example: replace_line(path="Daily/2026-07-23", line_text="//azure alert correlation",
                          content="**//azure alert correlation**\\n- ...")
    """
    v = get_vault()
    with v.locked():
        text = v.read(path)
        lines = text.splitlines()
        anchor = line_text.rstrip()
        hits = [i for i, ln in enumerate(lines) if ln.rstrip() == anchor]
        if not hits:
            raise VaultError(f"Line not found in {path}: {line_text!r}")
        if len(hits) > 1:
            raise VaultError(f"Line appears {len(hits)} times in {path}; must be unique: {line_text!r}")
        v.snapshot_before("replace_line")
        lines[hits[0] : hits[0] + 1] = content.splitlines()
        v.write(path, "\n".join(lines) + "\n")
        v.snapshot_after(f"replace line in {path}")
    return {"updated": path}


@mcp.tool()
def update_frontmatter(path: str, key: str, value: str) -> dict:
    """Set one frontmatter (YAML properties) field on a note. Value is parsed as
    YAML, so "true", "3", and "[a, b]" become their typed values.

    Example: update_frontmatter(path="Tasks/Vector DBs", key="status", value="in-progress")
    """
    v = get_vault()
    with v.locked():
        text = v.read(path)
        fm, body = md.split_frontmatter(text)
        v.snapshot_before("update_frontmatter")
        # MCP callers send strings (the schema says so), but in-process
        # proposal plans arrive from JSON where value may already be typed
        # (null, number, bool) — yaml.safe_load(None) crashes, and a crash
        # here once wedged the engine in an apply loop (2026-07-23).
        fm[key] = yaml.safe_load(value) if isinstance(value, str) else value
        v.write(path, md.join_frontmatter(fm, body))
        v.snapshot_after(f"set frontmatter {key} on {path}")
    return {"updated": path, "frontmatter": fm}


# --------------- tasks ---------------

def _note_type(fm: dict, rel_path: str) -> str:
    """Classify a note as an entity type: kanban boards by content, then the
    note's own frontmatter `type`, then the vault spec's folder→type map
    (config/vault.yaml — the one typing source), then the bare folder name."""
    if "kanban-plugin" in fm:
        return "kanban"
    if isinstance(fm.get("type"), str) and fm["type"]:
        return fm["type"].lower()
    parts = rel_path.split("/")
    if len(parts) < 2:
        return "other"
    return vc.get_config().folder_types.get(parts[0], parts[0].lower())


@mcp.tool(annotations=READ_ONLY)
def list_tasks(folder: str = "", status: str = "open", task_type: str = "",
               due_on: str = "", due_before: str = "", limit: int = 50) -> dict:
    """List checkbox tasks across the vault, grouped with per-type counts.
    Every task carries a `type` derived from its note: "kanban" for board
    cards, the note's frontmatter type (e.g. "meeting", "research"), or the
    entity type of its top-level folder (e.g. "task", "deliverable",
    "daily"; unmapped folders use the folder name). The result leads
    with `total` and `counts_by_type` — use those to answer "how many"
    questions. status: "open", "done", or "all". task_type narrows to one
    type. Date filters (both YYYY-MM-DD): due_on = tasks due exactly that
    date ("what's due tomorrow?" -> due_on=tomorrow's date); due_before =
    tasks due strictly before that date (for overdue checks).

    Example: list_tasks(status="open") ->
      {"filter": {"status": "open"}, "total": 22,
       "counts_by_type": {"task": 7, "kanban": 4, "research": 4, ...},
       "tasks": [{"note": "Tasks/Inbox.md", "type": "task", "text": ..., "due": ...}, ...]}
    """
    v = get_vault()
    if status not in ("open", "done", "all"):
        raise VaultError('status must be "open", "done", or "all"')
    found = []
    counts: dict[str, int] = {}
    for p in v.iter_notes(folder):
        text = p.read_text(encoding="utf-8")
        fm, _ = md.split_frontmatter(text)
        note_type = _note_type(fm, v.rel(p))
        for task in tk.iter_tasks(text):
            if status == "open" and task.done:
                continue
            if status == "done" and not task.done:
                continue
            if due_before and (task.due is None or task.due >= due_before):
                continue
            if due_on and task.due != due_on:
                continue
            if task_type and note_type != task_type.lower():
                continue
            counts[note_type] = counts.get(note_type, 0) + 1
            entry = task.to_dict(v.rel(p))
            entry["type"] = note_type
            found.append(entry)
    found.sort(key=lambda t: (t.get("due") or "9999", t["note"]))
    applied = {"status": status}
    for key, value in (("folder", folder), ("task_type", task_type),
                       ("due_on", due_on), ("due_before", due_before)):
        if value:
            applied[key] = value
    return {
        "filter": applied,
        "total": len(found),
        "counts_by_type": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "shown": min(len(found), limit),
        "tasks": found[:limit],
    }


@mcp.tool()
def add_task(path: str, text: str, due: str = "", priority: str = "", section: str = "") -> dict:
    """Add a checkbox task to a note. due is YYYY-MM-DD; priority is one of
    highest/high/medium/low/lowest. If `section` is given the task is added at
    the end of that heading's section, otherwise at the end of the note.

    Example: add_task(path="Tasks/Inbox", text="Renew domain", due="2026-07-25", priority="high")
    """
    line = tk.format_task(text, due or None, priority or None, block_id=ids.stamp_id("t"))
    if section:
        return append_to_note(path, line, section)
    # No section: append as a LIST CONTINUATION — a task after a task gets
    # no blank separator (the paragraph-break append was scattering every
    # captured task into its own island; 2026-07-23 dogfood report).
    v = get_vault()
    p = v.resolve(path)
    with v.locked():
        v.snapshot_before("add_task")
        if not p.exists():
            v.write(v.rel(p), line + "\n")
            v.snapshot_after(f"create note {v.rel(p)}")
            return {"created": v.rel(p)}
        fm, body = md.split_frontmatter(v.read(path))
        stripped = body.rstrip("\n")
        last = stripped.splitlines()[-1] if stripped else ""
        sep = "\n" if tk.TASK_RE.match(last) else "\n\n"
        body = (stripped + sep + line + "\n") if stripped else line + "\n"
        v.write(path, md.join_frontmatter(fm, body))
        v.snapshot_after(f"add task to {v.rel(p)}")
    return {"updated": v.rel(p)}


@mcp.tool()
def set_task_status(path: str, task_text: str, done: bool = True) -> dict:
    """Mark a task done (or reopen it with done=false). task_text matches the task
    by exact text or unique substring. Completing stamps today's ✅ done date.

    Example: set_task_status(path="Tasks/Inbox", task_text="Renew domain", done=true)
    """
    v = get_vault()
    # Read and locate the task under the lock: parsing outside it would let
    # a concurrent writer shift line numbers between read and write, and the
    # write-back of pre-lock lines would clobber their change.
    with v.locked():
        text = v.read(path)
        lines = text.splitlines()
        needle = task_text.strip().lower()
        hits = [t for t in tk.iter_tasks(text) if needle == t.text.lower()] or \
               [t for t in tk.iter_tasks(text) if needle in t.text.lower()]
        if not hits:
            raise VaultError(f"No task matching {task_text!r} in {path}")
        if len(hits) > 1:
            raise VaultError(f"Ambiguous task text; matches: {[t.text for t in hits[:5]]}")
        task = hits[0]
        line = lines[task.line_no]
        # Rebuild the checkbox from the parsed position: string replacement would
        # miss nonstandard marks like [/] and touch brackets in the task text.
        m = tk.TASK_RE.match(line)
        mark_at = m.start("mark")
        line = line[:mark_at] + ("x" if done else " ") + line[mark_at + 1 :]
        if done:
            if "✅" not in line:
                # A block ref must stay the last token, so ✅ slots in before it.
                core, ref = ids.split_block_ref(line)
                line = f"{core} ✅ {_today()}"
                if ref:
                    line = ids.attach_block_ref(line, ref)
        else:
            line = tk.DONE_RE.sub("", line).rstrip()
        v.snapshot_before("set_task_status")
        lines[task.line_no] = line
        v.write(path, "\n".join(lines) + "\n")
        v.snapshot_after(f"{'complete' if done else 'reopen'} task in {path}")
    return {"updated": path, "task": task.text, "done": done}


@mcp.tool(annotations=READ_ONLY)
async def extract_tasks(text: str) -> dict:
    """Pull actionable tasks out of free text (a meeting note, a brain dump,
    a paragraph) using a small dedicated model — not the model you're
    talking to. Read-only: returns suggestions, it does not write anything.
    Call add_task for the ones worth keeping.

    Use this instead of parsing prose yourself when tasks are buried in
    paragraphs rather than already formatted as checkboxes.

    Example: extract_tasks(text="Rio needs to migrate DNS by Friday, high
    priority. Also worth revisiting the pricing page at some point.") ->
      {"tasks": [{"text": "Migrate DNS", "due": "2026-07-24", "priority": "high"},
                 {"text": "Revisit the pricing page", "due": null, "priority": null}],
       "count": 2}
    """
    tasks = await wk.extract_tasks(text, _today())
    return {"tasks": tasks, "count": len(tasks)}


# --------------- entity schemas ---------------

def note_text_issues(rel: str, text: str) -> tuple[str, list[str], list[dict], bool]:
    """The per-note core of validate_schema, callable on any text —
    including a simulated post-fix version that isn't on disk yet.
    Returns (entity, note_issues, bad_tasks, unmarked)."""
    fm, body = md.split_frontmatter(text)
    etype = _note_type(fm, rel)
    note_issues = sc.validate_frontmatter(etype, fm)
    if etype == "research":
        note_issues = note_issues + sc.validate_research_body(body)
    bad_tasks, any_unstamped = [], False
    for task in tk.iter_tasks(body):
        t_issues = sc.validate_line(task, etype)
        if t_issues:
            bad_tasks.append({"line": task.line_no + 1, "text": task.text, "issues": t_issues})
            any_unstamped = any_unstamped or task.block_id is None
    has_note_schema = etype in sc.load_schemas() and etype not in sc.LINE_TYPES
    unmarked = (has_note_schema and not fm.get("id")) or any_unstamped
    return etype, note_issues, bad_tasks, unmarked


@mcp.tool(annotations=READ_ONLY)
def validate_schema(folder: str = "", limit: int = 50, offset: int = 0) -> dict:
    """Check notes and their tasks/cards against the vault's entity schemas
    (config/schemas/). Read-only: reports nonconformance, never changes
    anything. Entities with no id watermark have never been reviewed and
    sort first; stamped entities are still re-checked for drift. Use
    offset to page through a long report.

    Example: validate_schema(folder="Meetings") ->
      {"total_checked": 12, "nonconforming_count": 3, "shown": 3,
       "nonconforming": [{"note": "Meetings/2026-07-15 Sprint Planning.md",
                          "entity": "meeting", "unmarked": true,
                          "issues": ["(root): 'id' is a required property"],
                          "tasks": [{"line": 14, "text": "...", "issues": [...]}]}]}
    """
    v = get_vault()
    surface_dirs = vc.get_config().surface_dirs
    rows = []
    checked = 0
    for p in v.iter_notes(folder):
        rel = v.rel(p)
        if rel.split("/")[0] in surface_dirs:
            continue  # machine-rendered surfaces are not entities
        checked += 1
        etype, note_issues, bad_tasks, unmarked = note_text_issues(
            rel, p.read_text(encoding="utf-8"))
        if not note_issues and not bad_tasks:
            continue
        rows.append({
            "note": rel,
            "entity": etype,
            "unmarked": unmarked,
            **({"issues": note_issues} if note_issues else {}),
            **({"tasks": bad_tasks} if bad_tasks else {}),
        })
    rows.sort(key=lambda r: (not r["unmarked"], r["note"]))
    page = rows[offset : offset + limit]
    result = {
        "total_checked": checked,
        "nonconforming_count": len(rows),
        "shown": len(page),
        "nonconforming": page,
    }
    if offset + limit < len(rows):
        result["next_offset"] = offset + limit
    return result


# --------------- kanban boards ---------------

@mcp.tool()
def create_board(path: str, columns: str = "Backlog, Doing, Done") -> dict:
    """Create a new Kanban plugin board. columns is a comma-separated list of
    column names; a column named Done/Complete becomes the completion column.
    Fails if the board already exists.

    Example: create_board(path="Projects/Marketing.md", columns="Ideas, Active, Shipped")
    """
    v = get_vault()
    p = v.resolve(path)
    if p.exists():
        raise VaultError(f"Board already exists: {v.rel(p)}")
    names = [c.strip() for c in columns.split(",") if c.strip()]
    if not names:
        raise VaultError("At least one column name is required")
    board = kb.Board(
        frontmatter={"kanban-plugin": "board", "id": ids.stamp_id("b")},
        columns=[
            kb.Column(name=n, complete_marker=n.lower() in ("done", "complete", "completed"))
            for n in names
        ],
        settings="",
    )
    with v.locked():
        v.snapshot_before("create_board")
        v.write(v.rel(p), kb.render_board(board))
        v.snapshot_after(f"create board {v.rel(p)}")
    return {"created": v.rel(p), "columns": names}


@mcp.tool(annotations=READ_ONLY)
def read_board(path: str) -> dict:
    """Read a Kanban plugin board as structured columns and cards.

    Example: read_board(path="Projects/Project Board") ->
      {"columns": [{"name": "Backlog", "cards": [{"text": "...", "done": false}]}, ...]}
    """
    v = get_vault()
    return kb.parse_board(v.read(path)).to_dict()


@mcp.tool()
def add_card(path: str, column: str, text: str) -> dict:
    """Add a card to a column of a Kanban board.

    Example: add_card(path="Projects/Project Board", column="Backlog", text="Draft pricing page")
    """
    v = get_vault()
    with v.locked():
        board = kb.parse_board(v.read(path))
        v.snapshot_before("add_card")
        board.column(column).cards.append(
            {"text": text.strip(), "done": False, "id": ids.stamp_id("c")}
        )
        v.write(path, kb.render_board(board))
        v.snapshot_after(f"add card to {column!r} on {path}")
    return {"board": path, "column": column, "added": text.strip()}


@mcp.tool()
def move_card(path: str, card: str, to_column: str) -> dict:
    """Move a card (matched by exact text or unique substring) to another column.
    Moving into a column named Done/Complete also checks the card off.

    Example: move_card(path="Projects/Project Board", card="Draft pricing page", to_column="Doing")
    """
    v = get_vault()
    with v.locked():
        board = kb.parse_board(v.read(path))
        src, found = board.find_card(card)
        dest = board.column(to_column)
        v.snapshot_before("move_card")
        src.cards.remove(found)
        found["done"] = dest.complete_marker or dest.name.lower() in ("done", "complete", "completed")
        dest.cards.append(found)
        v.write(path, kb.render_board(board))
        v.snapshot_after(f"move card {found['text']!r} to {dest.name!r} on {path}")
    return {"board": path, "card": found["text"], "from": src.name, "to": dest.name}


# --------------- daily & meeting notes ---------------

@mcp.tool()
def open_daily_note(date: str = "") -> dict:
    """Get today's daily note (or the given YYYY-MM-DD date's), creating it from a
    standard skeleton if missing. Returns its path and current content.

    Example: open_daily_note() -> {"path": "Daily/2026-07-17.md", "content": "..."}
    """
    v = get_vault()
    day = _valid_date(date) if date else _today()
    rel = f"{vc.get_config().daily_dir}/{day}.md"
    created = not v.resolve(rel).exists()
    if created:
        with v.locked():
            v.snapshot_before("open_daily_note")
            v.write(rel, f"# {day}\n\n## Tasks\n\n## Notes\n")
            v.snapshot_after(f"create daily note {day}")
    return {"path": rel, "content": v.read(rel), "created": created}


@mcp.tool()
def create_meeting_note(title: str, date: str = "", attendees: str = "") -> dict:
    """Create a meeting note under Meetings/ using the vault's meeting template if
    one exists (Harness/Templates/Meeting.md), else a standard skeleton. attendees
    is a comma-separated list. Returns the new note's path.

    Example: create_meeting_note(title="Sprint Planning", date="2026-07-17", attendees="Sam, Rio")
    """
    v = get_vault()
    day = _valid_date(date) if date else _today()
    people = [a.strip() for a in attendees.split(",") if a.strip()]
    rel = f"{MEETINGS_DIR}/{day} {title.strip()}.md"
    p = v.resolve(rel)
    if p.exists():
        raise VaultError(f"Meeting note already exists: {rel}")
    try:
        template = md.split_frontmatter(v.read("Harness/Templates/Meeting.md"))[1]
        weekday = dt.date.fromisoformat(day).strftime("%A")
        body = (template.replace("{{title}}", title.strip())
                        .replace("{{date}}", day)
                        .replace("{{day}}", weekday)
                        .replace("{{time}}", dt.datetime.now().strftime("%H:%M"))
                        .replace("{{attendees}}", ", ".join(people)))
    except VaultError:
        attendee_lines = "".join(f"- {a}\n" for a in people)
        body = f"# {title.strip()}\n\n## Attendees\n\n{attendee_lines}\n## Agenda\n\n## Notes\n\n## Action items\n"
    fm = {"id": ids.stamp_id("m"), "date": day, "type": "meeting", "attendees": people}
    with v.locked():
        v.snapshot_before("create_meeting_note")
        v.write(rel, md.join_frontmatter(fm, body))
        v.snapshot_after(f"create meeting note {rel}")
    return {"created": rel}


def main() -> None:
    get_vault()  # fail fast if OBSIDIAN_VAULT_PATH is bad
    mcp.run()


if __name__ == "__main__":
    main()
