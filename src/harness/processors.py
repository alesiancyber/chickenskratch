"""Processor registry (DESIGN-processors.md §1, DEVPLAN B9).

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
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import yaml

from obsidian_mcp import ids
from obsidian_mcp import tasks as tk
from obsidian_mcp.vault import VaultError
from obsidian_mcp.vaultconfig import SPEC_REL, get_config

from . import applier, grouping, proposals
from .proposals import content_hash

if TYPE_CHECKING:
    from .watcher import Watcher

# entity type -> id prefix for the note-level watermark the drafter stamps
ID_PREFIX = {"meeting": "m", "research": "r", "kanban": "b"}

CAPTURE_SKELETON = """\
# Capture

_Type or paste anything here — brain dumps, meeting scraps, forwarded
messages. On its next pass the harness extracts tasks and proposes them
in Review.md; nothing is added to the vault without your approval there._
"""


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
    # User-invoked transformations (a typed //command IS the approval):
    # verified drafts apply immediately without needing Spec.md auto_apply
    # trust. The verifier and staleness checks still gate every apply —
    # only the trust list is bypassed, because the user asked by name.
    # disabled_processors still turns the whole processor off.
    user_invoked: bool = False


# ---- capture: Harness/Capture.md -> extraction -> inbox/waiting proposal ----


def ensure_capture(w: Watcher) -> None:
    """The capture surface must exist to be typed into."""
    if not w.capture_rel or w.vault.resolve(w.capture_rel).exists():
        return
    w.write_surface(w.capture_rel, CAPTURE_SKELETON,
                    f"create {w.capture_rel}", action="create_capture_surface")


def capture_text(raw: str) -> str:
    """What the user typed: the note minus the skeleton's own lines.
    Known //command lines are never capture text — a command typed here
    by habit must not be extracted into a junk task (commands execute in
    knowledge notes; surfaces are excluded from the command scan)."""
    skeleton = {ln for ln in CAPTURE_SKELETON.splitlines() if ln.strip()}
    return "\n".join(
        ln for ln in raw.splitlines()
        if ln.strip() and ln not in skeleton and not is_command_line(ln)
    ).strip()


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
    if not text or content_hash(text) == w._capture_seen:
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
    w._capture_seen = content_hash(item["text"])
    if not tasks:
        return None
    # Own actions land in the capture inbox; commitments other people
    # made ("Nadia will send X") are follow-ups, not the user's tasks —
    # they route to the waiting list.
    n_own = sum(1 for t in tasks if not t.get("owed_by"))
    n_owed = len(tasks) - n_own
    parts = ([f"{n_own} captured task(s) to {w.capture_inbox}"] if n_own else []) + (
        [f"{n_owed} waiting-on item(s) to {w.waiting_inbox}"] if n_owed else []
    )
    return proposals.new_proposal(
        kind="capture",
        note=item["note"],
        vault_sha=item["vault_sha"],
        note_content=item["content"],
        title="Add " + " + ".join(parts),
        summary=f"Extracted from {item['note']}; approving adds them and clears the surface.",
        issues=[
            (f"waiting on {t['owed_by']}: " if t.get("owed_by") else "")
            + t["text"]
            + (f" — due {t['due']}" if t.get("due") else "")
            + (f", {t['priority']}" if t.get("priority") else "")
            for t in tasks
        ],
        apply=[
            {"tool": "add_task", "args": {
                "path": w.waiting_inbox if t.get("owed_by") else w.capture_inbox,
                "text": f"{t['owed_by']}: {t['text']}" if t.get("owed_by") else t["text"],
                **({"due": t["due"]} if t.get("due") else {}),
                **({"priority": t["priority"]} if t.get("priority") else {}),
            }}
            for t in tasks
        ],
    )


def _capture_applied(w: Watcher, p: dict) -> None:
    if w.capture_rel:
        reset_capture(w)


# ---- inline_command: //azure typed anywhere -> results written in place ----
#
# The vault is the command line: a line starting with a known //command in
# any non-surface note is an explicit order, executed on the next pass and
# replaced in place by its result (settle time means never mid-keystroke).
# Only KNOWN command names match, so prose and code comments that happen
# to start with // are never touched. One command per note per pass; a
# note with several resolves over successive passes.

_CMD_RE = re.compile(r"^//([a-z][\w-]*)\s+(\S.*)$")
_CMD_PREFIX_RE = re.compile(r"^//([a-z][\w-]*)\b")


def is_command_line(line: str) -> bool:
    """Does this line start with a KNOWN command name (args or not)?
    Used by capture to keep commands out of task extraction."""
    m = _CMD_PREFIX_RE.match(line)
    return bool(m and m.group(1) in INLINE_COMMANDS)


async def _azure_docs(args: str, vault_path: str) -> str | None:
    """//azure: one ms-learn MCP search, digested to a ranked link list
    (mcp_client's docs digest — links, never summaries), model-free.
    None = transient failure (server unreachable, no search tool): the
    command line stays put and retries on the next scan."""
    from .bootstrap import DEFAULT_MCP
    from .mcp_client import MultiServerClient, load_mcp_config

    try:
        specs = load_mcp_config(DEFAULT_MCP, vault_path)
        async with MultiServerClient(specs, server_filter=["ms-learn"]) as client:
            tool = next((t for t in client.tools if "search" in t), None)
            if tool is None:
                return None
            text, is_error = await client.call(tool, {"query": args})
    except Exception:
        return None
    if is_error:
        return None
    lines = text.splitlines()
    if lines and lines[0].startswith("Documentation pages found"):
        lines = lines[1:]  # model-facing instruction, not note content
    # Cap the block at a LINE boundary: a mid-line cut would sever a link.
    kept, size = [], 0
    for ln in lines:
        size += len(ln) + 1
        if size > 6000:
            break
        kept.append(ln)
    return "\n".join(kept).strip() or None


# command name -> async (args, vault_path) -> result markdown | None.
# //research is reserved for a future web-crawling command.
INLINE_COMMANDS: dict[str, Callable[[str, str], Awaitable[str | None]]] = {
    "azure": _azure_docs,
}


def scan_commands(w: Watcher) -> list[dict]:
    surface_dirs = get_config(w.vault_path).surface_dirs
    items = []
    for p in w.vault.iter_notes():
        rel = w.vault.rel(p)
        if rel.split("/")[0] in surface_dirs:
            continue
        text = p.read_text(encoding="utf-8")
        lines = text.splitlines()
        # Enter is the submit key: a final line with no newline after it
        # is still being typed — never execute a half-typed command.
        if text and not text.endswith("\n"):
            lines = lines[:-1]
        for line in lines:
            m = _CMD_RE.match(line)
            if m and m.group(1) in INLINE_COMMANDS:
                items.append({
                    "kind": "inline_command",
                    "note": rel,
                    "content": text,
                    "vault_sha": w._pass_sha,
                    "line": line,
                    "command": m.group(1),
                    "args": m.group(2).strip(),
                })
                break  # one per note per pass (the queue invariant anyway)
    return items


async def draft_command(w: Watcher, item: dict) -> dict | None:
    result = await INLINE_COMMANDS[item["command"]](item["args"], w.vault_path)
    if result is None:
        return None  # transient failure: the line stays, retried next scan
    # The result rides in an Obsidian callout: a visually distinct titled
    # block in reading AND live-preview views, collapsible by hand, plain
    # text on disk. The title echoes the command (provenance) and, being
    # blockquoted, can never re-match _CMD_RE.
    quoted = "\n".join("> " + ln for ln in result.splitlines())
    content = f"> [!example] //{item['command']} {item['args']}\n{quoted}"
    return proposals.new_proposal(
        kind="inline_command",
        note=item["note"],
        vault_sha=item["vault_sha"],
        note_content=item["content"],
        title=f"//{item['command']} {item['args'][:60]}",
        summary="Typed command, executed in place.",
        issues=[],
        apply=[{"tool": "replace_line", "args": {
            "path": item["note"], "line_text": item["line"], "content": content,
        }}],
    )


def verify_command(w: Watcher, item: dict, proposal: dict) -> list[str]:
    """Deterministic: the plan may replace exactly the typed command line,
    nothing else. With user_invoked this verifier is what checkbox-free
    application rides on — keep it tautologically tight."""
    plan = proposal["apply"]
    if len(plan) != 1 or plan[0]["tool"] != "replace_line":
        return [f"plan must be exactly one replace_line, got {[s['tool'] for s in plan]}"]
    args = plan[0]["args"]
    failures = []
    if args.get("path") != item["note"]:
        failures.append(f"plan targets {args.get('path')!r}, not the note the command was typed in")
    if args.get("line_text") != item["line"]:
        failures.append("plan anchors to a line other than the typed command")
    return failures


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
    found = _inbox_prose(raw.split("---", 2)[-1] if raw.startswith("---") else raw)
    if found is None:
        return []
    heading, sec_lines, idx, prose = found
    if content_hash("\n".join(prose)) == w._inbox_seen:
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
    w._inbox_seen = content_hash("\n".join(item["prose"]))
    if not tasks:
        return None
    own = [t for t in tasks if not t.get("owed_by")]
    owed = [t for t in tasks if t.get("owed_by")]
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
    return proposals.new_proposal(
        kind="inbox_extract",
        note=item["note"],
        vault_sha=item["vault_sha"],
        note_content=item["content"],
        title=f"Turn {len(item['prose'])} free-text line(s) in {item['note']} into "
        + " + ".join(parts),
        summary="Loose lines become stamped tasks in place; existing tasks stay "
        "byte-identical. Reject and edit by hand if the reading is wrong.",
        issues=[
            (f"waiting on {t['owed_by']}: " if t.get("owed_by") else "")
            + t["text"]
            + (f" — due {t['due']}" if t.get("due") else "")
            + (f", {t['priority']}" if t.get("priority") else "")
            for t in tasks
        ],
        apply=[{"tool": "replace_section", "args": {
            "path": item["note"], "heading": item["section"], "content": content,
        }}] + [
            {"tool": "add_task", "args": {
                "path": w.waiting_inbox, "text": f"{t['owed_by']}: {t['text']}",
                **({"due": t["due"]} if t.get("due") else {}),
                **({"priority": t["priority"]} if t.get("priority") else {}),
            }}
            for t in owed
        ],
    )


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
    import difflib

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
    issues would re-draft forever, so it queues instead."""
    from obsidian_mcp import markdown as md
    from obsidian_mcp import server

    after = applier.simulate(item["content"], proposal)
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
    import pathlib

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
        items.append({
            "kind": "schema_fix",
            "note": row["note"],
            "entity": row["entity"],
            "issues": row.get("issues", [])
            + [f"line {t['line']}: {i}" for t in row.get("tasks", []) for i in t["issues"]],
            "content": w.vault.read(row["note"]),
            "vault_sha": w._pass_sha,
            "new_id": ids.stamp_id(ID_PREFIX.get(row["entity"], "n")),
            "extra_ids": [ids.stamp_id("t") for _ in range(len(row.get("tasks", [])))],
        })
    return items


async def draft_schema(w: Watcher, item: dict) -> dict | None:
    reply = await w.drafter(item)
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
    v = w.vault
    items = []
    for p in v.iter_notes():
        rel = v.rel(p)
        if rel.split("/")[0] in surface_dirs:
            continue
        text = p.read_text(encoding="utf-8")
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


# ---- the registry ----

CAPTURE = ProcessorSpec(
    "capture", scan_capture, draft_capture,
    render_diff=False, ensure=ensure_capture, on_applied=_capture_applied,
    verify=verify_extraction,
)
INLINE_COMMAND = ProcessorSpec("inline_command", scan_commands, draft_command,
                               verify=verify_command, user_invoked=True)
INBOX_EXTRACT = ProcessorSpec("inbox_extract", scan_inbox, draft_inbox,
                              verify=verify_extraction)
SPEC_DRIFT = ProcessorSpec("spec_drift", scan_spec_drift, draft_spec_drift)
SCHEMA_FIX = ProcessorSpec("schema_fix", scan_schema, draft_schema,
                           verify=verify_schema_fix)
GROUPING = ProcessorSpec("grouping", scan_grouping, draft_grouping)

# Typed input first (capture, then //commands): the user actively feeding
# the pipeline is never starved out of n_max by a maintenance backlog.
REGISTRY: tuple[ProcessorSpec, ...] = (
    CAPTURE, INLINE_COMMAND, INBOX_EXTRACT, SPEC_DRIFT, SCHEMA_FIX, GROUPING)


def registry(disabled: Sequence[str] = ()) -> tuple[ProcessorSpec, ...]:
    """The default processor set minus `disabled` names (the generalized
    rollback flag). Unknown names fail loud — a typo must not silently
    leave a processor running."""
    unknown = set(disabled) - {p.name for p in REGISTRY}
    if unknown:
        raise ValueError(f"unknown processor(s): {sorted(unknown)}")
    return tuple(p for p in REGISTRY if p.name not in disabled)
