"""chickenskratch vault MCP server.

Exposes semantic tools (tasks, kanban, meeting notes) alongside general
note tools (search, read, section edits) over a folder of markdown. Any
MCP client can use this server; the harness in this repo is just one of
them. Every mutation is bracketed by git snapshots so changes are
always revertible.

Run: CHICKENSKRATCH_VAULT_PATH=/path/to/vault chickenskratch-vault-mcp
"""

from __future__ import annotations

import datetime as dt
import re

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
from .vault import VaultError, get_vault, read_lossy

READ_ONLY = ToolAnnotations(readOnlyHint=True)

mcp = FastMCP("chickenskratch-vault")

# Daily- and meeting-note folders come from the vault spec (`daily_dir`,
# `meetings_dir`), so Foam's dailyNotes.directory and this harness can
# agree by config, not code.


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
        for i, line in enumerate(read_lossy(p).splitlines(), 1):
            if q in line.lower():
                matches.append({"note": rel, "line": i, "text": line.strip()[:200]})
    page = matches[offset : offset + limit]
    result = {"matches": page, "total": len(matches)}
    if offset + limit < len(matches):
        result["next_offset"] = offset + limit
    return result


# --------------- writing notes ---------------

def _writable(path: str, spec_key: str | None = None) -> str:
    """The vault spec (and any non-capture machine surface) is not
    tool-writable. Spec.md edits could lift the Private/ exclusion or
    extend auto_apply trust (I4) — config is the human's, edited in an
    editor; no model may widen its own trust list (I2). The engine
    writes its own surfaces through vault.write directly, so nothing
    legitimate routes here. Capture.md stays writable: it only feeds
    the proposal queue, so writing it can never bypass consent.

    One sanctioned exception: update_frontmatter on Spec.md's `folders`
    map — the spec_drift processor's approved proposals land there, and
    the key carries no trust (typing, not consent or exclusion)."""
    v = get_vault()
    cfg = vc.get_config()
    target = v.resolve(path)
    protected = [rel for key, rel in cfg.surfaces.items() if key != "capture" and rel]
    protected.append(vc.SPEC_REL)
    for rel in protected:
        if target == v.resolve(rel):
            if rel == vc.SPEC_REL and spec_key == "folders":
                return path
            raise VaultError(
                f"{rel} is machine-managed and not tool-writable. Config is "
                "the user's own Spec.md edit; decisions happen on the "
                "dashboard or via `chickenskratch decide`, never through a tool.")
    return path


@mcp.tool()
def create_note(path: str, content: str = "") -> dict:
    """Create a new note. Fails if the note already exists (use append_to_note or
    replace_section to change existing notes). Folders are created as needed.

    Example: create_note(path="Tasks/Vector DBs.md", content="# Vector DBs\\n\\n- ...")
    """
    _writable(path)
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
    _writable(path)
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
    _writable(path)
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
    _writable(path)
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
    _writable(path, spec_key=key)
    v = get_vault()
    with v.locked():
        text = v.read(path)
        fm, body = md.split_frontmatter(text)
        v.snapshot_before("update_frontmatter")
        # MCP callers send strings (the schema says so), but in-process
        # proposal plans arrive from JSON where value may already be typed
        # (null, number, bool) — yaml.safe_load(None) crashes, and a crash
        # here wedges the engine in an apply loop.
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
    entity type of its top-level folder (e.g. "task", "research",
    "daily"; unmapped folders use the folder name). The result leads
    with `total` and `counts_by_type` — use those to answer "how many"
    questions. status: "open", "done", or "all". task_type narrows to one
    type. Date filters (both YYYY-MM-DD): due_on = tasks due exactly that
    date ("what's due tomorrow?" -> due_on=tomorrow's date); due_before =
    tasks due strictly before that date (for overdue checks). A subtask
    (an indented task line) carries `depth`, `parent_text`, and — when
    the parent has a ^ref — `parent`; top-level tasks carry none of
    these.

    Example: list_tasks(status="open") ->
      {"filter": {"status": "open"}, "total": 22,
       "counts_by_type": {"task": 7, "kanban": 4, "research": 4, ...},
       "tasks": [{"note": "Captures/2026-07-17.md", "type": "task", "text": ..., "due": ...}, ...]}
    """
    v = get_vault()
    if status not in ("open", "done", "all"):
        raise VaultError('status must be "open", "done", or "all"')
    found = []
    counts: dict[str, int] = {}
    for p in v.iter_notes(folder):
        text = read_lossy(p)
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
def add_task(path: str, text: str, due: str = "", priority: str = "",
             section: str = "", scheduled: str = "", under: str = "") -> dict:
    """Add a checkbox task to a note. due (YYYY-MM-DD) is a HARD deadline;
    scheduled (YYYY-MM-DD) is the ⏳ plan date — "this week" / "next week"
    plans stamp the week's Sunday. priority is one of
    highest/high/medium/low/lowest. If `section` is given the task is added
    at the end of that heading's section, otherwise at the end of the note.
    `under` makes it a SUBTASK: the text of an existing task in the note
    (exact or unique substring, like set_task_status); the new line is
    indented beneath that task, after any subtasks it already has, and
    `section` is ignored.

    Example: add_task(path="Captures/2026-07-25", text="Renew domain", due="2026-07-25", priority="high")
    Example: add_task(path="Projects/Wiki", text="Export the space", under="Migrate the wiki")
    """
    _writable(path)
    line = tk.format_task(text, due or None, priority or None,
                          block_id=ids.stamp_id("t"), scheduled=scheduled or None)
    if under:
        return _add_subtask(path, line, under)
    if section:
        return append_to_note(path, line, section)
    # No section: append as a LIST CONTINUATION — a task after a task gets
    # no blank separator (a paragraph-break append scatters every captured
    # task into its own island).
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


def _add_subtask(path: str, line: str, under: str) -> dict:
    """Insert `line` as the last child of the task matching `under`:
    one level deeper than the parent (its existing children's indent
    when it has any, so a tab-indented note stays tab-indented), after
    the parent's whole block — every following deeper-indented task
    line and the blank-or-indented content between them."""
    v = get_vault()
    with v.locked():
        text = v.read(path)
        parent = _locate_task(text, under, path)
        lines = text.splitlines()
        # The block: every following line that is blank, indented
        # deeper than the parent, or a deeper task — the parent's
        # content, kanban's rule. Trailing blanks stay behind.
        end = parent.line_no + 1
        while end < len(lines):
            ln = lines[end]
            if ln.strip() and (not ln[0].isspace()
                               or len(ln[:len(ln) - len(ln.lstrip())].expandtabs(tk.TAB_WIDTH))
                               <= parent.indent):
                break
            end += 1
        while end > parent.line_no + 1 and not lines[end - 1].strip():
            end -= 1
        children = [t for t in tk.iter_tasks(text)
                    if t.parent_line == parent.line_no]
        prefix = (children[-1].raw[:len(children[-1].raw) - len(children[-1].raw.lstrip())]
                  if children else
                  parent.raw[:len(parent.raw) - len(parent.raw.lstrip())] + tk.INDENT)
        v.snapshot_before("add_task")
        lines.insert(end, prefix + line)
        v.write(path, "\n".join(lines) + "\n")
        v.snapshot_after(f"add subtask to {v.rel(v.resolve(path))}")
    return {"updated": v.rel(v.resolve(path)), "under": parent.text}


def _locate_task(text: str, task_text: str, path: str) -> tk.Task:
    """One task by exact text, falling back to unique substring — shared
    by every by-text task verb (set_task_status, set_task_due). Callers
    hold the vault lock: locating outside it would let a concurrent
    writer shift line numbers between read and write."""
    needle = task_text.strip().lower()
    hits = [t for t in tk.iter_tasks(text) if needle == t.text.lower()] or \
           [t for t in tk.iter_tasks(text) if needle in t.text.lower()]
    if not hits:
        raise VaultError(f"No task matching {task_text!r} in {path}")
    if len(hits) > 1:
        raise VaultError(f"Ambiguous task text; matches: {[t.text for t in hits[:5]]}")
    return hits[0]


@mcp.tool()
def set_task_status(path: str, task_text: str, done: bool = True) -> dict:
    """Mark a task done (or reopen it with done=false). task_text matches the task
    by exact text or unique substring. Completing stamps today's ✅ done date.

    Example: set_task_status(path="Captures/2026-07-25", task_text="Renew domain", done=true)
    """
    _writable(path)
    v = get_vault()
    with v.locked():
        text = v.read(path)
        lines = text.splitlines()
        task = _locate_task(text, task_text, path)
        # tasks.set_line_status is the one completion rewrite — the
        # editor's checkbox (/api/task/line) rides the same function.
        line = tk.set_line_status(lines[task.line_no], done, _today())
        v.snapshot_before("set_task_status")
        lines[task.line_no] = line
        v.write(path, "\n".join(lines) + "\n")
        v.snapshot_after(f"{'complete' if done else 'reopen'} task in {path}")
    return {"updated": path, "task": task.text, "done": done}


@mcp.tool()
def set_task_due(path: str, task_text: str, due: str = "") -> dict:
    """Set a task's 📅 due date, or clear it with due="". task_text
    matches by exact text or unique substring; everything else on the
    line (priority, ➕ created, the ^id) stays byte-identical. This is
    the reschedule verb — the dashboard's week→today move rides on it,
    and every call is one revertible snapshot.

    Example: set_task_due(path="Captures/2026-07-25", task_text="Renew domain", due="2026-07-28")
    """
    _writable(path)
    if due:
        due = _valid_date(due, "due")
    v = get_vault()
    with v.locked():
        text = v.read(path)
        lines = text.splitlines()
        task = _locate_task(text, task_text, path)
        new_line = tk.set_line_due(task.raw, due or None)
        if new_line == task.raw:
            return {"updated": path, "task": task.text, "due": due or None,
                    "previous_due": task.due, "line": task.raw, "written": False}
        v.snapshot_before("set_task_due")
        lines[task.line_no] = new_line
        v.write(path, "\n".join(lines) + "\n")
        v.snapshot_after(f"reschedule task in {path} to {due or 'no date'}")
    return {"updated": path, "task": task.text, "due": due or None,
            "previous_due": task.due, "line": new_line, "written": True}


@mcp.tool()
def set_task_scheduled(path: str, task_text: str, scheduled: str = "") -> dict:
    """Set a task's ⏳ scheduled date — "work on this then" — or clear it
    with scheduled="". This is the PLAN field, distinct from 📅 due (a
    hard external commitment, which this never touches): most tasks have
    no real deadline, so promoting one into today or pushing it later is
    a judgment, recorded as ⏳. The dashboard's promote/defer verbs ride
    on this; every call is one revertible snapshot.

    Example: set_task_scheduled(path="Captures/2026-07-25", task_text="Renew domain", scheduled="2026-07-27")
    """
    _writable(path)
    if scheduled:
        scheduled = _valid_date(scheduled, "scheduled")
    v = get_vault()
    with v.locked():
        text = v.read(path)
        lines = text.splitlines()
        task = _locate_task(text, task_text, path)
        new_line = tk.set_line_scheduled(task.raw, scheduled or None)
        if new_line == task.raw:
            return {"updated": path, "task": task.text, "scheduled": scheduled or None,
                    "previous_scheduled": task.scheduled, "line": task.raw,
                    "written": False}
        v.snapshot_before("set_task_scheduled")
        lines[task.line_no] = new_line
        v.write(path, "\n".join(lines) + "\n")
        v.snapshot_after(f"plan task in {path} for {scheduled or 'no date'}")
    return {"updated": path, "task": task.text, "scheduled": scheduled or None,
            "previous_scheduled": task.scheduled, "line": new_line, "written": True}


@mcp.tool()
def archive_done_tasks(path: str, archive_path: str = "") -> dict:
    """Move every completed TOP-LEVEL task from a note into the archive
    note (vault spec `done_archive` by default), one atomic snapshot. A
    task moves as its whole block: any indented sub-content (notes,
    sub-bullets, nested subtasks) travels with it, byte-identical — ids,
    dates, priorities, indentation intact. A done subtask under an open
    parent stays put with that parent. A task completed by hand in an
    editor (no ✅ date) gets today's stamped on, so the archive always
    says when. Removing lines never leaves blank runs behind.

    Example: archive_done_tasks(path="Captures/2026-07-25") ->
      {"archived": 3, "archive": "Tasks/Archive.md"}
    """
    v = get_vault()
    _writable(path)
    archive = (archive_path or vc.get_config().done_archive).strip()
    if not archive:
        raise VaultError("no archive configured (vault spec `done_archive` is empty)")
    _writable(archive)
    if v.resolve(path) == v.resolve(archive):
        return {"archived": 0, "archive": archive}  # never archive the archive
    with v.locked():
        text = v.read(path)
        fm, body = md.split_frontmatter(text)
        done = [t for t in tk.iter_tasks(body) if t.done and not t.indent]
        if not done:
            return {"archived": 0, "archive": archive}
        v.snapshot_before("archive_done_tasks")
        lines = body.splitlines()
        moved: list[str] = []
        claimed: set[int] = set()
        for t in done:
            # set_line_status back-fills the missing ✅ (a hand-typed [x])
            # — the same one completion rewrite every other surface uses.
            line = t.raw if t.done_date else tk.set_line_status(t.raw, True, _today())
            # The task's block: every following blank-or-indented line
            # belongs to it and moves with it (trailing blanks are just
            # separation and stay behind to be collapsed).
            block = [t.line_no]
            j = t.line_no + 1
            while j < len(lines) and (not lines[j].strip() or lines[j][:1].isspace()):
                block.append(j)
                j += 1
            while len(block) > 1 and not lines[block[-1]].strip():
                block.pop()
            claimed.update(block)
            moved.append(line)
            moved.extend(lines[i] for i in block[1:])
        keep = [ln for i, ln in enumerate(lines) if i not in claimed]
        new_body = re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).rstrip("\n") + "\n"
        v.write(path, md.join_frontmatter(fm, new_body))
        ap = v.resolve(archive)
        existing = v.read(archive) if ap.exists() else "# Archive\n"
        stripped = existing.rstrip("\n")
        last = stripped.splitlines()[-1] if stripped else ""
        sep = "\n" if tk.TASK_RE.match(last) else "\n\n"
        v.write(archive, stripped + sep + "\n".join(moved) + "\n")
        v.snapshot_after(f"archive {len(done)} done task(s) from {path}")
    return {"archived": len(done), "archive": archive}


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

def nonconforming_rows(texts) -> list[dict]:
    """validate_schema's core over (rel, text) pairs, never-reviewed
    entities first. The MCP tool below walks the vault and delegates
    here; the engine hands its per-pass note cache instead, so schema
    validation costs no walk of its own."""
    rows = []
    for rel, text in texts:
        etype, note_issues, bad_tasks, unmarked = note_text_issues(rel, text)
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
    return rows


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
    checked = 0

    def walked():
        nonlocal checked
        for p, rel in v.iter_knowledge_notes(surface_dirs, folder):
            checked += 1
            yield rel, read_lossy(p)

    rows = nonconforming_rows(walked())
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
    _writable(path)
    v = get_vault()
    p = v.resolve(path)
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
        # Existence under the lock (same rule as create_note): two
        # concurrent creators must collide here, not silently overwrite.
        if p.exists():
            raise VaultError(f"Board already exists: {v.rel(p)}")
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
    _writable(path)
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
    _writable(path)
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
    with v.locked():
        # Existence under the lock: a concurrent creator's content must
        # never be clobbered by this skeleton.
        created = not v.resolve(rel).exists()
        if created:
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
    rel = f"{vc.get_config().meetings_dir}/{day} {title.strip()}.md"
    p = v.resolve(rel)
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
        # Existence under the lock (same rule as create_note): two
        # concurrent creators must collide here, not silently overwrite.
        if p.exists():
            raise VaultError(f"Meeting note already exists: {rel}")
        v.snapshot_before("create_meeting_note")
        v.write(rel, md.join_frontmatter(fm, body))
        v.snapshot_after(f"create meeting note {rel}")
    return {"created": rel}


def main() -> None:
    get_vault()  # fail fast if CHICKENSKRATCH_VAULT_PATH is bad
    mcp.run()


if __name__ == "__main__":
    main()
