"""Processor registry.

A processor is one transformation in two stages: `scan` is deterministic
code that turns vault state into work items, `draft` is at most one
bounded worker call that turns a work item into a proposal. Processors
never write — the applier and the engine's surface hooks are the only
vault-touching code paths. The engine (watcher.Watcher) iterates its
registry in order; capture sits first so active user input is never
starved out of n_max by a maintenance backlog.
"""

from __future__ import annotations

import datetime as dt
import difflib
import pathlib
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

from obsidian_mcp import ids
from obsidian_mcp import markdown as md
from obsidian_mcp import tasks as tk
from obsidian_mcp.vault import VaultError, read_lossy
from obsidian_mcp.vaultconfig import SPEC_REL, get_config

from . import applier, grouping, proposals

# Re-exported: the capture text contract is shared with applier (see
# capture.py), but it reads as part of this processor's surface.
from .capture import CAPTURE_SKELETON, capture_text
from .commands import is_command_line
from .proposals import content_hash

__all__ = ["CAPTURE_SKELETON", "capture_text", "registry", "REGISTRY"]

if TYPE_CHECKING:
    from .watcher import Watcher

# entity type -> id prefix for the note-level watermark the drafter stamps
ID_PREFIX = {"meeting": "m", "research": "r", "kanban": "b"}

@dataclass(frozen=True)
class ProcessorSpec:
    """One transformation; `name` doubles as the proposal `kind`."""

    name: str
    scan: Callable[[Watcher], list[dict]]
    draft: Callable[[Watcher, dict], Awaitable[dict | None]]
    render_diff: bool = True  # False: the issues list IS the payload; no diff block
    ensure: Callable[[Watcher], None] | None = None  # pass-start hook (create surfaces)
    on_applied: Callable[[Watcher, dict], None] | None = None  # post-apply hook
    # Deterministic safety check enabling auto-apply (Spec.md `auto_apply`):
    # (w, item, proposal) -> failure strings. Empty = safe to apply without
    # a checkbox; any failure (or no verifier) = the Review.md queue.
    verify: Callable[[Watcher, dict, dict], list[str]] | None = None


# ---- capture: Harness/Capture.md -> extraction -> inbox/waiting proposal ----


def ensure_capture(w: Watcher) -> None:
    """The capture surface must exist to be typed into."""
    if not w.capture_rel or w.vault.resolve(w.capture_rel).exists():
        return
    w.write_surface(w.capture_rel, CAPTURE_SKELETON,
                    f"create {w.capture_rel}", action="create_capture_surface")


def reset_capture(w: Watcher) -> None:
    """After an approved capture lands, clear the surface for the next
    dump. The text lives on in the proposal file and in git history."""
    w.write_surface(w.capture_rel, CAPTURE_SKELETON,
                    f"clear {w.capture_rel} after capture landed",
                    action="reset_capture_surface")


def scan_capture(w: Watcher) -> list[dict]:
    if not w.capture_rel:
        return []
    try:
        raw = w.vault.read(w.capture_rel)
    except VaultError:
        return []
    text = capture_text(raw)
    if not text or content_hash(text) == w.seen.get("capture"):
        return []
    return [{
        "kind": "capture",
        "note": w.capture_rel,
        "content": raw,
        "text": text,
        "vault_sha": w._pass_sha,
    }]


async def draft_capture(w: Watcher, item: dict) -> dict | None:
    tasks = await w.capture_extractor(item["text"], dt.date.today().isoformat())
    # Processed regardless of outcome: the same text is never
    # re-extracted; editing the surface changes the hash and re-arms.
    w.seen["capture"] = content_hash(item["text"])
    if not tasks:
        return None
    own, owed = _split_owed(tasks)
    parts = ([f"{len(own)} captured task(s) to {w.capture_inbox}"] if own else []) + (
        [f"{len(owed)} waiting-on item(s) to {w.waiting_inbox}"] if owed else []
    )
    p = proposals.new_proposal(
        kind="capture",
        note=item["note"],
        vault_sha=item["vault_sha"],
        # Hash the stripped TEXT, not the raw note: extraction only ever
        # sees the text, so staleness must key on it too. Hashing raw let
        # a whitespace-only autosave between scan and approval void the
        # user's approval — and the unchanged text hash then blocked the
        # re-arm, silently dropping the task (2026-07-24). is_stale
        # normalizes the same way for capture proposals.
        note_content=item["text"],
        title="Add " + " + ".join(parts),
        summary=f"Extracted from {item['note']}; approving adds them and clears the surface.",
        issues=_extraction_issues(tasks),
        apply=[
            _add_task_step(w.waiting_inbox if t.get("owed_by") else w.capture_inbox, t)
            for t in tasks
        ],
    )
    return _flag_dropped_dates(p, tasks)


def _capture_applied(w: Watcher, p: dict) -> None:
    if w.capture_rel:
        reset_capture(w)


# ---- shared extraction shaping (capture and inbox_extract) ----
#
# Both extractors run the same worker over user text and turn its tasks
# into the same three things: a reviewable issues list, add_task steps,
# and an own/owed split. Only WHERE the text comes from and what else
# the plan does differ, so the shaping lives here once.


def _split_owed(tasks: list[dict]) -> tuple[list[dict], list[dict]]:
    """(own actions, things other people owe). Commitments someone else
    made ("Nadia will send X") are follow-ups, not the user's tasks —
    they route to the waiting list."""
    own = [t for t in tasks if not t.get("owed_by")]
    return own, [t for t in tasks if t.get("owed_by")]


def _task_issue(t: dict) -> str:
    """One extracted task as a Review.md line — what the user is being
    asked to approve, dates and priorities included."""
    return (
        (f"waiting on {t['owed_by']}: " if t.get("owed_by") else "")
        + t["text"]
        + (f" — due {t['due']}" if t.get("due") else "")
        + (f", {t['priority']}" if t.get("priority") else "")
    )


def _extraction_issues(tasks: list[dict]) -> list[str]:
    return [_task_issue(t) for t in tasks] + _dropped_date_issues(tasks)


def _add_task_step(path: str, t: dict) -> dict:
    """One add_task apply step. A task someone else owes carries their
    name in the text, so the waiting list reads "who: what"."""
    return {"tool": "add_task", "args": {
        "path": path,
        "text": f"{t['owed_by']}: {t['text']}" if t.get("owed_by") else t["text"],
        **({"due": t["due"]} if t.get("due") else {}),
        **({"priority": t["priority"]} if t.get("priority") else {}),
    }}


def _dropped_date_issues(tasks: list[dict]) -> list[str]:
    return [
        f"⚠ impossible due date {t['dropped_due']!r} was dropped from {t['text']!r} — set it by hand"
        for t in tasks if t.get("dropped_due")
    ]


def _flag_dropped_dates(p: dict, tasks: list[dict]) -> dict:
    """A worker date the sanitizer had to drop means the extraction lost
    information the fidelity verifier can't see (dates live in structured
    fields, not text) — so the proposal forces its checkbox regardless of
    what the verifier says."""
    if any(t.get("dropped_due") for t in tasks):
        p["needs_review"] = True
    return p


# ---- inbox_extract: free text typed into the task inbox -> real tasks ----


def _inbox_prose(body: str) -> tuple[str, list[str], list[int], list[str]] | None:
    """First section of the note containing free-text lines (non-blank,
    not a heading, not a checkbox). Returns (heading, section_lines,
    prose_indices, prose_lines) or None."""
    heading = ""
    sec_lines: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in body.splitlines():
        if line.lstrip().startswith("#") and line.lstrip("#") != line:
            sections.append((heading, sec_lines))
            heading, sec_lines = line.lstrip("# ").strip(), []
        else:
            sec_lines.append(line)
    sections.append((heading, sec_lines))
    for head, lines in sections:
        if not head:
            continue  # v1: only prose under a heading
        idx = [i for i, ln in enumerate(lines) if _is_task_intent(ln)]
        if idx:
            return head, lines, idx, [lines[i] for i in idx]
    return None


def _is_task_intent(ln: str) -> bool:
    """Free text worth extracting: non-blank, not already a checkbox, and
    not reference material — //command lines, callout/blockquote lines
    (command results render as callouts), URL lines, and indented
    continuations are content, never task intent (a //azure result
    written into the inbox must not be re-extracted as tasks)."""
    if not ln.strip() or ln.lstrip().startswith("- ["):
        return False
    if ln != ln.lstrip():
        return False  # indented continuation (e.g. a result's "covers:" line)
    if ln.startswith(">"):
        return False  # blockquote/callout: quoted reference, not intent
    return not (is_command_line(ln) or ln.startswith("**//") or "://" in ln)


def scan_inbox(w: Watcher) -> list[dict]:
    """Free text typed straight into the capture inbox — the same promise
    as Capture.md (write anywhere natural, the harness proposes), scoped
    v1 to the one note where loose lines are unambiguously task intent."""
    try:
        raw = w.vault.read(w.capture_inbox)
    except VaultError:
        return []
    # The real frontmatter parser, not a hand-rolled split on "---": a note
    # that OPENS with a horizontal rule (no frontmatter at all) made that
    # split eat its first line, and the section indices below are derived
    # from whatever body it returned.
    found = _inbox_prose(md.split_frontmatter(raw)[1])
    if found is None:
        return []
    heading, sec_lines, idx, prose = found
    if content_hash("\n".join(prose)) == w.seen.get("inbox_extract"):
        return []  # rejected text is never re-drafted; editing re-arms
    return [{
        "kind": "inbox_extract",
        "note": w.capture_inbox,
        "content": raw,
        "vault_sha": w._pass_sha,
        "section": heading,
        "sec_lines": sec_lines,
        "prose_idx": idx,
        "prose": prose,
    }]


async def draft_inbox(w: Watcher, item: dict) -> dict | None:
    text = "\n".join(ln.lstrip("- ").strip() for ln in item["prose"])
    tasks = await w.capture_extractor(text, dt.date.today().isoformat())
    w.seen["inbox_extract"] = content_hash("\n".join(item["prose"]))
    if not tasks:
        return None
    own, owed = _split_owed(tasks)
    skip = set(item["prose_idx"])
    kept = [ln for i, ln in enumerate(item["sec_lines"]) if i not in skip]
    while kept and not kept[-1].strip():
        kept.pop()
    new_lines = [
        tk.format_task(t["text"], t.get("due"), t.get("priority"), block_id=ids.stamp_id("t"))
        for t in own
    ]
    content = "\n".join([*kept, *new_lines]).strip("\n")
    parts = ([f"{len(own)} task(s)"] if own else []) + (
        [f"{len(owed)} waiting-on item(s) to {w.waiting_inbox}"] if owed else []
    )
    p = proposals.new_proposal(
        kind="inbox_extract",
        note=item["note"],
        vault_sha=item["vault_sha"],
        note_content=item["content"],
        title=f"Turn {len(item['prose'])} free-text line(s) in {item['note']} into "
        + " + ".join(parts),
        summary="Loose lines become stamped tasks in place; existing tasks stay "
        "byte-identical. Reject and edit by hand if the reading is wrong.",
        issues=_extraction_issues(tasks),
        apply=[{"tool": "replace_section", "args": {
            "path": item["note"], "heading": item["section"], "content": content,
        }}] + [_add_task_step(w.waiting_inbox, t) for t in owed],
    )
    return _flag_dropped_dates(p, tasks)


# ---- extraction verifier: the fidelity gate that makes auto-apply safe ----

_WORD_RE = re.compile(r"[a-z0-9']+")

# Grammatical filler the model may add in `close` fidelity — connective
# tissue only, no content words.
_FILLER = {
    "the", "a", "an", "to", "of", "and", "or", "for", "with", "about", "on",
    "in", "at", "is", "are", "be", "it", "this", "that", "my", "our", "up",
    "from", "into", "by",
}
# Never addable at any fidelity: one of these appearing in the output but
# not the input flips or broadens meaning while staying "close" by every
# distance metric. Hard fail, no exceptions.
_MEANING_FLIPPERS = {
    "not", "no", "never", "dont", "don't", "cant", "can't", "wont", "won't",
    "stop", "cancel", "delete", "remove", "all", "every", "everything",
    "none", "instead", "except",
}


def _words(s: str) -> set[str]:
    return set(_WORD_RE.findall(s.lower()))


def _norm(s: str) -> str:
    return " ".join(_WORD_RE.findall(s.lower()))


def _morphological(token: str, source: set[str]) -> bool:
    """'detections' from 'detection', 'follows' from 'follow': the added
    token shares a substantial prefix with a word the user wrote and is
    close in length (an inflection, not a new concept)."""
    return any(
        len(s) >= 4 and len(token) >= 4
        and token[:4] == s[:4]
        and abs(len(s) - len(token)) <= 3
        for s in source
    )


def _close_check(produced: str, source_lines: list[str], source_words: set[str]) -> list[str]:
    """`close` fidelity: grammar and punctuation may improve, meaning may
    not. Layered: flippers never; additions must be filler or morphology;
    the produced text must sequence-match its best source line."""
    added = _words(produced) - source_words
    flip = added & _MEANING_FLIPPERS
    if flip:
        failures = [f"meaning-flipping word(s) {sorted(flip)} added in {produced!r}"]
        return failures
    bad = {t for t in added if t not in _FILLER and not _morphological(t, source_words)}
    if bad:
        return [f"invented word(s) {sorted(bad)} in {produced!r}"]
    norm_p = _norm(produced)
    if not norm_p:
        return [f"empty task text from {produced!r}"]
    coverage = max(
        (sum(b.size for b in difflib.SequenceMatcher(
            None, norm_p, _norm(line)).get_matching_blocks()) / len(norm_p)
         for line in source_lines if line.strip()),
        default=0.0,
    )
    if coverage < 0.7:
        return [f"only {coverage:.0%} of {produced!r} traces to any source line"]
    return []


def _check_text(produced: str, item: dict, fidelity: str) -> list[str]:
    source_text = item.get("text") or "\n".join(item.get("prose", []))
    source_words = _words(source_text)
    if fidelity == "strict":
        invented = _words(produced) - source_words
        return [f"invented word(s) {sorted(invented)} in {produced!r}"] if invented else []
    return _close_check(produced, source_text.splitlines(), source_words)


def verify_extraction(w: Watcher, item: dict, proposal: dict) -> list[str]:
    """Deterministic fidelity check for extraction proposals: every task
    the model produced must trace to the user's own words (dates and
    priorities live in structured fields, not text; Spec.md `fidelity`
    picks strict word-for-word vs a bounded grammar allowance), and any
    rewritten section must keep every pre-existing task line
    byte-identical. A failure here doesn't block anything — the proposal
    just keeps its checkbox."""
    fidelity = get_config(w.vault_path).fidelity
    failures = []
    for step in proposal["apply"]:
        if step["tool"] == "add_task":
            failures += _check_text(step["args"]["text"], item, fidelity)
        elif step["tool"] == "replace_section":
            new_lines = step["args"]["content"].splitlines()
            old_tasks = [ln for ln in item.get("sec_lines", []) if ln.lstrip().startswith("- [")]
            for ln in old_tasks:
                if ln not in new_lines:
                    failures.append(f"pre-existing task line dropped or altered: {ln!r}")
            for ln in new_lines:
                if ln in old_tasks or not ln.lstrip().startswith("- ["):
                    continue
                task = tk.parse_task_line(ln)
                if task is None:
                    failures.append(f"unparseable task line {ln!r}")
                else:
                    failures += _check_text(task.text, item, fidelity)
        else:
            failures.append(f"unexpected tool {step['tool']!r} in an extraction plan")
    return failures


# ---- additive-shape verifier: schema fixes that only add structure ----

_BLOCK_REF_RE = re.compile(r"\s*\^[a-z]-[a-z0-9]+\s*$")


def _strip_refs(body: str) -> list[str]:
    """Non-blank body lines with trailing block refs removed — the text
    the user actually wrote, independent of stamping."""
    return [_BLOCK_REF_RE.sub("", ln) for ln in body.splitlines() if ln.strip()]


def _is_subsequence(needle: list[str], hay: list[str]) -> tuple[bool, list[str]]:
    """Is needle an in-order subsequence of hay? Returns (ok, added) where
    added is every hay line not consumed by the match."""
    it = iter(needle)
    want = next(it, None)
    added = []
    for ln in hay:
        if want is not None and ln == want:
            want = next(it, None)
        else:
            added.append(ln)
    return want is None, added


def verify_schema_fix(w: Watcher, item: dict, proposal: dict) -> list[str]:
    """A schema fix may add structure — ids, required frontmatter keys,
    skeleton headings — but may not touch a word the user wrote. This is
    checkable for every entity type at once because 'shape' is data
    (config/schemas/): simulate the plan, then require (a) every original
    body line byte-identical modulo appended block refs, in order,
    (b) added lines are headings only, (c) existing frontmatter values
    unchanged, and (d) the result fully conformant — a fix that leaves
    issues would re-draft forever, so it queues instead. First gate:
    every step must target the drafted note — the worker authors full
    tool args, and "nothing touched but the target" is only provable
    when the target is pinned in code, not in the prompt."""
    from obsidian_mcp import server

    for step in proposal["apply"]:
        target = step.get("args", {}).get("path")
        if target is None or not applier.same_note(str(target), item["note"]):
            return [f"step {step.get('tool')!r} targets {target!r}, not the drafted note {item['note']!r}"]
    after = applier.simulate(item["content"], proposal, note=item["note"])
    if after is None:
        return ["plan not simulable — cannot vouch for its effect"]
    fm_b, body_b = md.split_frontmatter(item["content"])
    fm_a, body_a = md.split_frontmatter(after)
    failures = []
    for k, v in fm_b.items():
        if fm_a.get(k) != v:
            failures.append(f"frontmatter {k!r} changed from {v!r} to {fm_a.get(k)!r}")
    ok, added = _is_subsequence(_strip_refs(body_b), _strip_refs(body_a))
    if not ok:
        failures.append("a line the user wrote was altered, reworded, or dropped")
    failures += [
        f"added non-structural line: {ln!r}"
        for ln in added if not ln.lstrip().startswith("#")
    ]
    _etype, note_issues, bad_tasks, _ = server.note_text_issues(item["note"], after)
    if note_issues or bad_tasks:
        failures.append("fix leaves the note nonconforming — would re-draft every pass")
    return failures


# ---- spec_drift: the vault moved, the spec didn't -> spec proposal ----


def scan_spec_drift(w: Watcher) -> list[dict]:
    """Top-level folders holding notes that the vault spec doesn't know.
    One work item covers them all — the spec note is one note, and the
    queue allows one pending proposal per note."""
    cfg = get_config(w.vault_path)
    known = set(cfg.folder_types) | set(cfg.exclude) | set(cfg.surface_dirs)
    new = []
    for child in sorted(pathlib.Path(w.vault_path).iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name in known:
            continue
        if not any(child.rglob("*.md")):
            continue  # empty scaffolding isn't drift
        new.append(child.name)
    if not new:
        return []
    try:
        content = w.vault.read(SPEC_REL)
    except VaultError:
        return []  # no spec note yet: the engine seeds it next pass
    return [{
        "kind": "spec_drift",
        "note": SPEC_REL,
        "content": content,
        "vault_sha": w._pass_sha,
        "folders": new,
    }]


async def draft_spec_drift(w: Watcher, item: dict) -> dict | None:
    """Deterministic draft — no worker. New folders map to `other` (the
    untyped default); a real entity type is a hand edit to Spec.md, which
    is exactly where that decision belongs."""
    cfg = get_config(w.vault_path)
    merged = {**cfg.folder_types, **dict.fromkeys(item["folders"], "other")}
    names = ", ".join(item["folders"])
    return proposals.new_proposal(
        kind="spec_drift",
        note=item["note"],
        vault_sha=item["vault_sha"],
        note_content=item["content"],
        title=f"Add {len(item['folders'])} new folder(s) to the vault spec: {names}",
        summary="Maps them as type `other`. Want a real entity type instead? "
        "Edit the folders property in Harness/Spec.md and reject this.",
        issues=[f"top-level folder '{f}' is not in the vault spec" for f in item["folders"]],
        apply=[{"tool": "update_frontmatter", "args": {
            "path": item["note"], "key": "folders",
            "value": yaml.safe_dump(merged, default_flow_style=False, sort_keys=True),
        }}],
    )


# ---- schema_fix: nonconforming entities -> minimal-fix proposal ----


def scan_schema(w: Watcher) -> list[dict]:
    """Deterministic work items per nonconforming note. Harness/ and
    Private/ are excluded by the validate_schema implementation itself
    (the engine points obsidian_mcp at this vault at pass start);
    coverage dedupe and sha stamping are engine context."""
    from obsidian_mcp import server

    items = []
    for row in server.validate_schema(limit=10_000)["nonconforming"]:
        try:
            content = w.vault.read(row["note"])
        except VaultError:
            # Per NOTE, not per processor. validate_schema walks with
            # read_lossy, so it happily reports a mis-encoded note that
            # this strict read then rejects — and the engine catches scan
            # failures at the PROCESSOR level, so one latin-1 note (a
            # paste out of Outlook is enough) silently disabled schema
            # fixing entirely, every pass, forever. Skipping the note
            # costs that note its fix; raising cost every note theirs.
            # Strict is still right here: this content is the base for a
            # read-modify-write, and replacement chars must never be
            # written back.
            continue
        items.append({
            "kind": "schema_fix",
            "note": row["note"],
            "entity": row["entity"],
            "issues": row.get("issues", [])
            + [f"line {t['line']}: {i}" for t in row.get("tasks", []) for i in t["issues"]],
            "content": content,
            "vault_sha": w._pass_sha,
            "new_id": ids.stamp_id(ID_PREFIX.get(row["entity"], "n")),
            "extra_ids": [ids.stamp_id("t") for _ in range(len(row.get("tasks", [])))],
        })
    return items


async def draft_schema(w: Watcher, item: dict) -> dict | None:
    reply = await w.drafter(item)
    # A schema fix has no legitimate cross-note step, so a wrong path is a
    # worker malfunction — never a proposal (the grouping rule: broken
    # output dies here, not in the queue where an inattentive approve
    # could still execute it). verify_schema_fix re-checks this for
    # defense in depth on the auto-apply path.
    for step in reply["apply"]:
        target = step.get("args", {}).get("path")
        if target is None or not applier.same_note(str(target), item["note"]):
            return None
    return proposals.new_proposal(
        kind="schema_fix",
        note=item["note"],
        vault_sha=item["vault_sha"],
        note_content=item["content"],
        title=reply["title"],
        summary=reply["summary"],
        issues=item["issues"],
        apply=reply["apply"],
    )


# ---- grouping: flat stamped task lists -> partition proposal ----


def scan_grouping(w: Watcher) -> list[dict]:
    """Groupable flat task lists (grouping.py's structural rules), one
    work item per note. Surfaces are machine-rendered, not knowledge —
    never candidates; Private/ is invisible to iter_notes already."""
    surface_dirs = get_config(w.vault_path).surface_dirs
    items = []
    for p, rel in w.vault.iter_knowledge_notes(surface_dirs):
        text = read_lossy(p)
        cand = grouping.note_candidate(text)
        if cand is None:
            continue
        items.append({
            "kind": "grouping",
            "note": rel,
            "content": text,
            "vault_sha": w._pass_sha,
            **cand,  # section, level, tasks
        })
    return items


async def draft_grouping(w: Watcher, item: dict) -> dict | None:
    reply = await w.grouping_drafter(item)
    member_ids = [t.block_id for t in item["tasks"]]
    groups = reply.get("groups", [])
    if grouping.validate_partition(groups, member_ids) or len(groups) < 2:
        return None  # broken partition or nothing gained: never a proposal
    content = grouping.render_grouped(
        item["level"], groups, {t.block_id: t.raw for t in item["tasks"]}
    )
    return proposals.new_proposal(
        kind="grouping",
        note=item["note"],
        vault_sha=item["vault_sha"],
        note_content=item["content"],
        title=f"Group {len(member_ids)} tasks under '{item['section']}' in {item['note']}",
        summary=f"{len(groups)} groups: " + ", ".join(str(g["name"]) for g in groups),
        issues=[],
        apply=[{"tool": "replace_section", "args": {
            "path": item["note"], "heading": item["section"], "content": content,
        }}],
        entity_ids=member_ids,
    )


# ---- archive_done: completed tasks -> the archive note ----


def scan_archive(w: Watcher) -> list[dict]:
    """Done tasks sitting in the working inboxes — the lists whose hygiene
    the harness owns. Tasks completed inside knowledge notes (meeting
    action items) stay in place: they're historical record."""
    cfg = get_config(w.vault_path)
    if not cfg.done_archive:
        return []
    items = []
    for rel in dict.fromkeys((cfg.capture_inbox, cfg.waiting_inbox)):
        if rel == cfg.done_archive:
            continue
        try:
            text = w.vault.read(rel)
        except VaultError:
            continue
        # Top-level only, mirroring the archive tool: a done subtask under
        # an open parent stays with its parent, so it is not archivable work.
        done = [t for t in tk.iter_tasks(text) if t.done and t.raw.lstrip() == t.raw]
        if done:
            items.append({"kind": "archive_done", "note": rel, "content": text,
                          "vault_sha": w._pass_sha, "archive": cfg.done_archive,
                          "tasks": [t.text for t in done]})
    return items


async def draft_archive(w: Watcher, item: dict) -> dict | None:
    """Deterministic draft — no worker; the semantic tool owns the move."""
    n = len(item["tasks"])
    return proposals.new_proposal(
        kind="archive_done",
        note=item["note"],
        vault_sha=item["vault_sha"],
        note_content=item["content"],
        title=f"Archive {n} done task(s) from {item['note']}",
        summary=f"Completed tasks move byte-identical to {item['archive']}; "
        "hand-checked ones gain today's ✅ date.",
        issues=[f"done: {t}" for t in item["tasks"]],
        apply=[{"tool": "archive_done_tasks",
                "args": {"path": item["note"], "archive_path": item["archive"]}}],
    )


def verify_archive(w: Watcher, item: dict, proposal: dict) -> list[str]:
    """Shape check — the transformation itself is deterministic code (the
    archive tool), so like a command's verifier this only pins that the
    plan is exactly one archive step aimed where the scan looked."""
    plan = proposal["apply"]
    if len(plan) != 1 or plan[0]["tool"] != "archive_done_tasks":
        return ["plan must be exactly one archive_done_tasks step"]
    args = plan[0]["args"]
    failures = []
    if args.get("path") != item["note"]:
        failures.append("plan targets a different note")
    if args.get("archive_path") != item["archive"]:
        failures.append("plan targets a different archive")
    return failures


# ---- the registry ----

CAPTURE = ProcessorSpec(
    "capture", scan_capture, draft_capture,
    render_diff=False, ensure=ensure_capture, on_applied=_capture_applied,
    verify=verify_extraction,
)
INBOX_EXTRACT = ProcessorSpec("inbox_extract", scan_inbox, draft_inbox,
                              verify=verify_extraction)
SPEC_DRIFT = ProcessorSpec("spec_drift", scan_spec_drift, draft_spec_drift)
SCHEMA_FIX = ProcessorSpec("schema_fix", scan_schema, draft_schema,
                           verify=verify_schema_fix)
GROUPING = ProcessorSpec("grouping", scan_grouping, draft_grouping)
ARCHIVE_DONE = ProcessorSpec("archive_done", scan_archive, draft_archive,
                             render_diff=False, verify=verify_archive)

# Capture first: the user actively feeding the pipeline is never starved
# out of n_max by a maintenance backlog. archive_done outranks schema/
# grouping — the one-proposal-per-note invariant means whoever scans a
# note first claims it, and grouping was claiming the inbox (a stamped
# flat list is a grouping candidate) while done tasks sat unarchived;
# housekeeping first, then fix/group what remains. (//commands are not
# processors — they execute in their own pass phase; see commands.py.)
REGISTRY: tuple[ProcessorSpec, ...] = (
    CAPTURE, INBOX_EXTRACT, ARCHIVE_DONE, SPEC_DRIFT, SCHEMA_FIX, GROUPING)


def registry(disabled: Sequence[str] = ()) -> tuple[ProcessorSpec, ...]:
    """The default processor set minus `disabled` names (the generalized
    rollback flag). Unknown names fail loud — a typo must not silently
    leave a processor running."""
    unknown = set(disabled) - {p.name for p in REGISTRY}
    if unknown:
        raise ValueError(f"unknown processor(s): {sorted(unknown)}")
    return tuple(p for p in REGISTRY if p.name not in disabled)
