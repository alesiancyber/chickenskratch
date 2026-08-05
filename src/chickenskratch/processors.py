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

from chickenskratch_vault import ids
from chickenskratch_vault import markdown as md
from chickenskratch_vault import schemas as sc
from chickenskratch_vault import tasks as tk
from chickenskratch_vault.vault import VaultError
from chickenskratch_vault.vaultconfig import SPEC_REL, get_config

from . import applier, grouping, proposals

# Re-exported: the capture text contract is shared with applier (see
# capture.py), but it reads as part of this processor's surface.
from .capture import CAPTURE_SKELETON, capture_text
from .proposals import content_hash

__all__ = ["CAPTURE_SKELETON", "capture_text", "registry", "REGISTRY"]

if TYPE_CHECKING:
    from .watcher import Watcher

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
    # a human verdict; any failure (or no verifier) = the review queue.
    verify: Callable[[Watcher, dict, dict], list[str]] | None = None
    # Whether this processor's work item claims its note in the
    # one-pending-proposal-per-note invariant. True for note-rewriting
    # processors (two pending diffs against one note would conflict).
    # False for the CROSS-note lanes (overlap, plan_today): their pool
    # spans the vault and the item's `note` is a bookkeeping anchor, so
    # letting it claim a real record note would starve that note's own
    # processors (grouping, schema) behind a proposal that barely
    # concerns it. Their steps carry their own staleness instead
    # (replace_line's exactly-once anchor, set_task_scheduled's
    # match-by-text), both of which fail loud when the line moved.
    claims_note: bool = True
    # How this processor introduces itself in the console. A registry entry
    # is the ONLY place that knows what it does, so it says so here rather
    # than the UI keeping a second list that drifts — a new processor
    # arrives already explained, or arrives visibly unexplained.
    title: str = ""
    blurb: str = ""

    @property
    def label(self) -> str:
        return self.title or self.name


# ---- capture: Harness/Capture.md -> decomposition -> record proposal ----
#
# A dump is a RECORD that happens to contain work (dumps-become-records,
# phase 1). One worker call decomposes it into {record, tasks}; the
# proposal files ONE note — a meeting record or a dated capture note —
# holding the summary rewrite, the task lines (owed ones carry 👤), and
# the verbatim dump folded beneath. Tasks are never moved to an inbox:
# location IS provenance, and Today's walk already reads every note.


def ensure_capture(w: Watcher) -> None:
    """The capture surface must exist to be typed into."""
    if not w.capture_rel or w.vault.resolve(w.capture_rel).exists():
        return
    w.write_surface(w.capture_rel, CAPTURE_SKELETON,
                    f"create {w.capture_rel}", action="create_capture_surface")


def reset_capture(w: Watcher) -> None:
    """After an approved capture lands, clear the surface for the next
    dump. The text lives on, verbatim, inside the record that just
    landed (and in git history)."""
    w.write_surface(w.capture_rel, CAPTURE_SKELETON,
                    f"clear {w.capture_rel} after capture landed",
                    action="reset_capture_surface")


def capture_archive_note(w: Watcher, today: dt.date | None = None) -> str:
    """The dated capture note — the record for a dump with no shape worth
    naming. One note per day, appended to.

    The verbatim always rides in whole, never line surgery. Measuring
    which lines the tasks "used" was tried and abandoned: a dump's dates,
    owners and priorities leave the text for STRUCTURED FIELDS, so "nadia
    will send the contract; review it in two weeks" scores lower on
    textual overlap with its own two tasks than a line that is half
    prose. There is no threshold that separates them, and every wrong
    guess in that direction deletes someone's writing.
    """
    cfg = get_config(w.vault_path)
    folder = (cfg.capture_archive or "").strip("/ ")
    if not folder:
        return ""
    return f"{folder}/{(today or dt.date.today()).isoformat()}.md"


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


def _quote(text: str) -> list[str]:
    """`text` as blockquote lines: every line `> `-prefixed, blanks a
    bare `>` so the quote stays one block. The quoting is mechanism, not
    styling — a quoted line can never match TASK_RE, the command scan,
    or inbox task intent (render_callout's rule), so the verbatim can
    never be re-extracted or walked as a duplicate of the tasks built
    from it."""
    return [("> " + ln).rstrip() for ln in text.splitlines()]


def _verbatim_callout(dump: str, stamp: str) -> list[str]:
    """The dump, whole, as a collapsed NOTE callout (`[!NOTE]-` folds in
    Obsidian and the owned editor; anywhere else it renders as a plain
    blockquote). Phase 0's guarantee carried forward: nothing the user
    typed is deleted, and checking the rewrite against the source is one
    unfold, not a git dig."""
    return [f"> [!NOTE]- Verbatim — {stamp}", *_quote(dump)]


def _record_task_lines(tasks: list[dict]) -> list[str]:
    """Extracted tasks as stamped checkbox lines — the record's live
    half. Owed items carry 👤 (owed-ness is a field now, not a folder)."""
    return [
        tk.format_task(t["text"], t.get("due"), t.get("priority"),
                       block_id=ids.stamp_id("t"), scheduled=t.get("scheduled"),
                       owner=t.get("owed_by"))
        for t in tasks
    ]


def _note_record(w: Watcher, rk: sc.RecordKind, record: dict, own: list[dict],
                 owed: list[dict], dump: str, now: dt.datetime) -> tuple[dict, str]:
    """(apply step, rel) creating a record note for a `files_as: note`
    kind — its folder and id prefix come from the kind's schema file, so
    a new kind files itself with zero code. The path is uniquified at
    draft time; a collision at apply time (created in the gap) fails the
    proposal loudly — create_note never overwrites."""
    cfg = get_config(w.vault_path)
    date = now.date().isoformat()
    title = (record["title"] or rk.name).replace("/", "-")
    folder = str(getattr(cfg, rk.dir_key) or "").strip("/")
    rel = f"{folder}/{date} {title}.md"
    if w.vault.resolve(rel).exists():
        rel = f"{folder}/{date} {title} {now.strftime('%H%M')}.md"
    fm = {"id": ids.stamp_id(sc.id_prefix(rk.name)), "type": rk.name, "date": date}
    # attendees only when the kind's entity schema declares the property —
    # a standup knows people, a clipping does not.
    if "attendees" in (sc.load_schemas().get(rk.name, {}).get("properties") or {}):
        fm["attendees"] = record["attendees"]
    body: list[str] = [""]
    if record["summary"]:
        body += ["## Summary", "", record["summary"], ""]
    if own:
        body += ["## Action items", "", *_record_task_lines(own), ""]
    if owed:
        body += ["## Waiting on", "", *_record_task_lines(owed), ""]
    body += [*_verbatim_callout(dump, now.strftime("%H:%M")), ""]
    content = md.join_frontmatter(fm, "\n".join(body))
    return {"tool": "create_note", "args": {"path": rel, "content": content}}, rel


def _capture_record(w: Watcher, record: dict, own: list[dict], owed: list[dict],
                    dump: str, now: dt.datetime) -> tuple[dict, str]:
    """(apply step, rel) appending a timestamped block to the dated
    capture note — the record for a dump with no shape worth naming
    ("where does a pure task list live? in the day you dumped it")."""
    # A blank capture_archive can't opt out anymore: with no inbox, the
    # dated note is where a shapeless dump LIVES, not a side archive.
    rel = capture_archive_note(w, now.date()) or f"Captures/{now.date().isoformat()}.md"
    stamp = now.strftime("%H:%M")
    # The block heading carries the dump's subject: a daily note whose
    # filename is only a date needs its content to say what the day was
    # about — the rail's label reads these topics back, so "which day
    # did I work detections" is answerable at a glance.
    title = record["title"].replace("\n", " ").strip()
    block: list[str] = [f"## {stamp} — {title}" if title else f"## {stamp}"]
    if record["summary"]:
        block += ["", record["summary"]]
    if own or owed:
        block += ["", *_record_task_lines(own + owed)]
    block += ["", *_verbatim_callout(dump, stamp)]
    content = "\n".join(block)
    if not w.vault.resolve(rel).exists():
        # append_to_note creates a missing note with the content as-is,
        # so the daily note's frontmatter rides in with the first block.
        # (A note created in the draft→apply gap would land this
        # mid-file: rare, visible on review, one revert.)
        content = f"---\ntype: capture\ndate: {now.date().isoformat()}\n---\n\n{content}"
    return {"tool": "append_to_note", "args": {"path": rel, "content": content}}, rel


async def draft_capture(w: Watcher, item: dict) -> dict | None:
    data = await w.record_worker(item["text"], dt.date.today().isoformat())
    # Processed regardless of outcome: the same text is never
    # re-extracted; editing the surface changes the hash and re-arms.
    w.seen["capture"] = content_hash(item["text"])
    record, tasks = data["record"], data["tasks"]
    tasks = _name_prefix_owed(record, tasks, item["text"])
    own, owed = _split_owed(tasks)
    now = dt.datetime.now()
    kinds = sc.record_kinds()
    rk = kinds.get(record["kind"]) or kinds.get(sc.default_record_kind())
    if rk is not None and rk.files_as == "note":
        step, rel = _note_record(w, rk, record, own, owed, item["text"], now)
    else:
        step, rel = _capture_record(w, record, own, owed, item["text"], now)
    # The task lines are held to extraction fidelity against the dump —
    # at draft time, into the issues the reviewer reads, because a
    # record never auto-applies and the checkbox is where warnings
    # matter. The summary rewrite is deliberately unchecked: prose has
    # no verifier; the folded verbatim is its check.
    fidelity = get_config(w.vault_path).fidelity
    warnings = [f"⚠ {f}" for t in tasks
                for f in _check_text(t["text"], item, fidelity)]
    parts = ([f"{len(own)} task(s)"] if own else []) + (
        [f"{len(owed)} waiting-on item(s)"] if owed else [])
    p = proposals.new_proposal(
        kind="capture",
        note=item["note"],
        vault_sha=item["vault_sha"],
        # Hash the stripped TEXT, not the raw note: extraction only ever
        # sees the text, so staleness must key on it too. Hashing raw
        # lets a whitespace-only autosave between scan and approval void
        # the user's approval — and the unchanged text hash then blocks
        # the re-arm, silently dropping the task. is_stale normalizes
        # the same way for capture proposals.
        note_content=item["text"],
        title=f"File to {rel}" + (f" — {' + '.join(parts)}" if parts else ""),
        summary=("The dump becomes a record: the summary is a rewrite (check "
                 "it against the verbatim folded at the record's foot), the "
                 "tasks live inside the record, and the surface clears."),
        issues=_extraction_issues(tasks) + warnings,
        apply=[step],
    )
    return _flag_dropped_dates(p, tasks)


def _capture_applied(w: Watcher, p: dict) -> None:
    """Clear the surface once its record landed. The record itself
    carries the dump verbatim (quoted, collapsed), so there is no
    separate archive write — filing IS the apply step, and clearing
    without filing is impossible by construction.

    A PARTIAL approval leaves the surface alone: the text behind any
    declined step is still the only copy."""
    if not w.capture_rel or p.get("partial"):
        return
    reset_capture(w)


# ---- extraction shaping: worker task rows -> reviewable pieces ----


def _split_owed(tasks: list[dict]) -> tuple[list[dict], list[dict]]:
    """(own actions, things other people owe). Commitments someone else
    made ("Nadia will send X") are follow-ups, not the user's tasks —
    they route to the waiting list."""
    own = [t for t in tasks if not t.get("owed_by")]
    return own, [t for t in tasks if t.get("owed_by")]


# A speaker-labeled line ("Sam: we should…") — how transcripts (VTT
# pastes included) name the people in the room, deterministically.
# At most three name-shaped words: "Sam Jones" is a speaker, a prose
# lead-in like "action items for me:" is not.
_SPEAKER_RE = re.compile(r"^([A-Za-z][\w'-]*(?: [A-Za-z][\w'-]*){0,2}):\s", re.M)
# The reader's own aliases: a "me:" or "User:" prefix is the user's task,
# never a debt someone else owes them.
_SELF_NAMES = {"me", "i", "user", "myself"}


def _name_prefix_owed(record: dict, tasks: list[dict], dump: str) -> list[dict]:
    """The owed_by backstop. Small workers reliably return the owner as a
    name PREFIX in the task text ("Rio: finish DNS migration") with
    owed_by null — which files somebody else's debt as the user's own
    work, invisible to the waiting view (owed-ness IS the 👤 field).
    When a task opens with a name we can independently see in the
    meeting — an extracted attendee, or a speaker label in the dump
    itself — that prefix is the owed_by field: move it there and drop it
    from the text (removal, so fidelity still traces every word to the
    dump). An unknown prefix is left alone: better an owned task than a
    debt invented for "TODO"."""
    names = {a.strip().lower(): a.strip()
             for a in record.get("attendees") or [] if a and a.strip()}
    for m in _SPEAKER_RE.finditer(dump):
        name = m.group(1).strip()
        names.setdefault(name.lower(), name)
    for alias in _SELF_NAMES:
        names.pop(alias, None)
    for t in tasks if names else []:
        if t.get("owed_by"):
            continue
        head, sep, rest = str(t.get("text", "")).partition(":")
        owner = names.get(head.strip().lower())
        if sep and owner and rest.strip():
            t["owed_by"] = owner
            t["text"] = rest.strip()
    return tasks


def _task_issue(t: dict) -> str:
    """One extracted task as a reviewable issue line — what the user is
    being asked to approve, dates and priorities included."""
    return (
        (f"waiting on {t['owed_by']}: " if t.get("owed_by") else "")
        + t["text"]
        + (f" — due {t['due']}" if t.get("due") else "")
        + (f" — planned for week of {t['scheduled']}" if t.get("scheduled") else "")
        + (f", {t['priority']}" if t.get("priority") else "")
    )


def _extraction_issues(tasks: list[dict]) -> list[str]:
    return [_task_issue(t) for t in tasks] + _dropped_date_issues(tasks)


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


# ---- record verifier: what makes a record auto-fileable ----

_HHMM_HEADING_RE = re.compile(r"^## \d{2}:\d{2}(?: — (?P<topic>.+))?$")
_RECORD_HEADINGS = {"## Summary", "## Action items", "## Waiting on"}


def _verify_record_frontmatter(fm: dict, item: dict) -> list[str]:
    """The frontmatter's model-filled parts, checked against the dump:
    attendees must be named in it (id/type/date are code-generated; the
    title lives in the path, visible and cheap to be wrong about).

    Allowed keys derive from the record's own entity schema — config may
    add kinds with their own properties, but a key no schema declares
    still fails. The checks themselves stay code: config can never
    weaken them."""
    if not fm:
        return []
    allowed = {"id", "type", "date"} | set(
        sc.load_schemas().get(str(fm.get("type") or ""), {}).get("properties") or {})
    failures = [f"unexpected frontmatter key {k!r}" for k in sorted(set(fm) - allowed)]
    src = _words(item["text"])
    for a in fm.get("attendees") or []:
        if not _words(str(a)) <= src:
            failures.append(f"attendee {a!r} is not named in the dump")
    return failures


def verify_record(w: Watcher, item: dict, proposal: dict) -> list[str]:
    """Deterministic checks for a record plan — the gate that decides
    how autonomous the capture lane gets to be.

    Checkable, and checked: one atomic step; every task line traces to
    the dump (extraction fidelity); the verbatim rides in COMPLETE,
    every line quoted; attendees are named in the dump. The summary is
    the one thing nothing deterministic can vouch for, so any body line
    that is not a task, a heading, or the quoted verbatim fails as
    model prose.

    What that buys, per Spec.md posture: a bare task-list dump — no
    rewrite, faithful tasks, verbatim complete — auto-files even under
    gated autonomy. A dump that earned a summary queues under gated,
    and under `autonomy: full` it files anyway with the prose objection
    on the resolved proposal — the folded verbatim is the human check,
    one unfold away, and every apply is one revertible snapshot."""
    fidelity = get_config(w.vault_path).fidelity
    if len(proposal["apply"]) != 1:
        return ["a record plan must be one atomic step"]
    step = proposal["apply"][0]
    if step["tool"] not in ("create_note", "append_to_note"):
        return [f"unexpected tool {step['tool']!r} in a record plan"]
    fm, body = md.split_frontmatter(str(step["args"].get("content", "")))
    failures = _verify_record_frontmatter(fm, item)
    quoted = set(_quote(item["text"]))
    seen_quoted: set[str] = set()
    for ln in body.splitlines():
        s = ln.strip()
        if not s:
            continue
        task = tk.parse_task_line(ln)
        if task is not None:
            failures += _check_text(task.text, item, fidelity)
            continue
        if ln.startswith(">"):
            if ln in quoted:
                seen_quoted.add(ln)
            elif not s.startswith("> [!NOTE]-"):
                failures.append(f"unexpected quoted line: {ln[:60]!r}")
            continue
        hm = _HHMM_HEADING_RE.match(s)
        if hm:
            # The heading's topic is model text naming the dump's subject
            # — a label, so it is held to the same fidelity as a task:
            # its words must trace to the dump, or the record queues.
            if hm.group("topic"):
                failures += _check_text(hm.group("topic"), item, fidelity)
            continue
        if s in _RECORD_HEADINGS:
            continue
        failures.append(f"summary prose no check can vouch for: {s[:60]!r}")
    failures += [f"verbatim line missing from the record: {q[:60]!r}"
                 for q in sorted(quoted - seen_quoted)]
    return failures


# ---- additive-shape verifier: schema fixes that only add structure ----

def _strip_refs(body: str) -> list[str]:
    """Non-blank body lines with trailing HARNESS refs removed — the text
    the user actually wrote, independent of stamping. ids.TYPED_REF_RE on
    purpose (not BLOCK_REF_RE): a hand-made ^myref is user text and must
    survive the strip."""
    return [ids.TYPED_REF_RE.sub("", ln) for ln in body.splitlines() if ln.strip()]


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
    from chickenskratch_vault import server

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
    """Deterministic work items per nonconforming note, validated over
    the pass's shared note cache (Harness/ and Private/ never enter it);
    coverage dedupe and sha stamping are engine context."""
    from chickenskratch_vault import server

    items = []
    for row in server.nonconforming_rows(w.pass_notes().items()):
        try:
            content = w.vault.read(row["note"])
        except VaultError:
            # Per NOTE, not per processor. The cache reads lossily, so it
            # happily reports a mis-encoded note that this strict read
            # then rejects — and the engine catches scan failures at the
            # PROCESSOR level, so one latin-1 note (a paste out of
            # Outlook is enough) would silently disable schema fixing
            # entirely, every pass. Skipping the note costs that note its
            # fix; raising costs every note theirs. Strict is still right
            # here: this content is the base for a read-modify-write, and
            # replacement chars must never be written back.
            continue
        items.append({
            "kind": "schema_fix",
            "note": row["note"],
            "entity": row["entity"],
            "issues": row.get("issues", [])
            + [f"line {t['line']}: {i}" for t in row.get("tasks", []) for i in t["issues"]],
            "content": content,
            "vault_sha": w._pass_sha,
            "new_id": ids.stamp_id(sc.id_prefix(row["entity"])),
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
    work item per note, over the pass's shared note cache (surfaces are
    machine-rendered, not knowledge — never in it; Private/ likewise)."""
    items = []
    for rel, text in w.pass_notes().items():
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


# ---- overlap: duplicates and links across notes ----
#
# The pattern: build the boundary in code, let similarity rank inside
# it, and let one bounded judge call decide — never more. Exact
# duplicates (normalized-identical text) are found deterministically;
# near-matches come from the embedding index (content-hash cached, so
# an unchanged vault re-embeds nothing) and are judged in ONE worker
# call: duplicate (remove the redundant copy), related (add a ⇄ link —
# links, not prose), or distinct (drop it). Verdicts queue — judgment
# has no verifier — and pair verdicts are remembered via the queue
# itself (pending AND resolved), so a rejected pair stops nagging and a
# pending one is never filed twice (until queue retention expires it).

OVERLAP_SIM_THRESHOLD = 0.80  # cosine floor for a pair to be worth judging
MAX_OVERLAP_PAIRS = 4         # judged pairs per pass — a trickle, not an audit

_NORM_RE = re.compile(r"[^a-z0-9 ]+")


def _norm_text(text: str) -> str:
    return " ".join(_NORM_RE.sub(" ", text.lower()).split())


def _metadata_score(t) -> int:
    return int(bool(t.due)) + int(bool(t.scheduled)) + int(bool(t.priority))


def _linked_line(raw: str, target_note: str, target_id: str) -> str:
    """`raw` with a ⇄ block link to the other task appended — before the
    trailing ^ref, which must stay the last token."""
    core, ref = ids.split_block_ref(raw)
    line = f"{core} ⇄ [[{target_note.removesuffix('.md')}#^{target_id}]]"
    return ids.attach_block_ref(line, ref) if ref else line


def _decided_pairs(w: Watcher) -> set[frozenset]:
    """Pairs already answered — or currently awaiting a verdict: never
    re-ask either. The queue is the memory: overlap proposals carry
    their pairs, a PENDING one means the question is already on the
    dashboard (re-drafting it would file the same pair twice), and
    queue retention (30d) is the natural expiry of a dismissal."""
    decided = set()
    for p in proposals.load_all(w.proposals_dir):
        if p.get("kind") != "overlap":
            continue
        for pair in p.get("pairs", []):
            if len(pair) == 2:
                decided.add(frozenset(pair))
    return decided


def scan_overlap(w: Watcher) -> list[dict]:
    """The comparison pool: every open, top-level, stamped task across
    the knowledge notes (the pass cache — no walk of its own). Exact
    duplicates are paired here in code; near-match pairing needs the
    embedding index and lives in the draft, where async belongs. The
    seen-hash keys on the whole pool, so an unchanged vault never
    re-embeds or re-judges."""
    pool: list[tuple] = []  # (Task, rel)
    for rel, text in w.pass_notes().items():
        for t in tk.iter_tasks(text):
            if t.done or t.raw.lstrip() != t.raw or not t.block_id:
                continue
            pool.append((t, rel))
    if len(pool) < 2:
        return []
    key = content_hash("\n".join(f"{rel}\t{t.block_id}\t{t.text}" for t, rel in sorted(
        pool, key=lambda x: (x[1], x[0].block_id))))
    if key == w.seen.get("overlap"):
        return []
    decided = _decided_pairs(w)
    by_norm: dict[str, list] = {}
    for t, rel in pool:
        norm = _norm_text(t.text)
        if norm:
            by_norm.setdefault(norm, []).append((t, rel))
    exact = []
    for _norm, group in by_norm.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if frozenset((a[0].block_id, b[0].block_id)) in decided:
                    continue
                exact.append((a, b))
    # Anchored on the first candidate pair's note (else the pool's first,
    # for the fuzzy-only case). Used to anchor on the capture inbox and
    # return [] when it couldn't be read — with the inbox retired, that
    # would have silently killed overlap for good, a missing note reading
    # as an empty vault. The real per-step staleness is replace_line's
    # exactly-once anchor, which fails loud if any target line moved.
    anchor = exact[0][0][1] if exact else min(rel for _t, rel in pool)
    return [{
        "kind": "overlap",
        "note": anchor,
        "content": w.pass_notes().get(anchor, ""),
        "vault_sha": w._pass_sha,
        "pool": pool,
        "exact": exact[:MAX_OVERLAP_PAIRS],
        "decided": decided,
        "seen_key": key,
    }]


async def _fuzzy_candidates(w: Watcher, item: dict) -> list[tuple] | None:
    """Near-match pairs from the embedding index, best first, bounded.
    None when the endpoint is down — exact duplicates and the rest of
    the pass never depend on the embedder (I6), but the caller must
    know the difference between "no candidates" and "couldn't look":
    only an answered pool may stamp the seen-hash, or an embedder
    outage silences the fuzzy lane until the pool happens to change."""
    from chickenskratch.embeddings import cosine

    pool = item["pool"]
    texts = {t.block_id: t.text for t, _rel in pool}
    try:
        vecs = await w.embedder(w.vault_path, texts)
    except Exception as e:  # noqa: BLE001 — degrade, never fail
        print(f"overlap: embeddings unavailable, exact-only this pass ({e})")
        return None
    by_id = {t.block_id: (t, rel) for t, rel in pool}
    exact_ids = {frozenset((a[0].block_id, b[0].block_id)) for a, b in item["exact"]}
    scored = []
    ids_list = [i for i in texts if i in vecs]
    for i in range(len(ids_list)):
        for j in range(i + 1, len(ids_list)):
            a, b = ids_list[i], ids_list[j]
            pair = frozenset((a, b))
            if pair in item["decided"] or pair in exact_ids:
                continue
            ta, ra = by_id[a]
            tb, rb = by_id[b]
            # already linked either way: the ⇄ is the durable verdict
            if a in tb.raw or b in ta.raw:
                continue
            sim = cosine(vecs[a], vecs[b])
            if sim >= OVERLAP_SIM_THRESHOLD:
                scored.append((sim, by_id[a], by_id[b]))
    scored.sort(key=lambda x: -x[0])
    return [(a, b) for _s, a, b in scored[:MAX_OVERLAP_PAIRS]]


def _overlap_steps(verdicts: list[dict]) -> tuple[list, list, list]:
    """(apply steps, issues, pairs) from judged verdicts — one step per
    actionable pair, 1:1 with issues so partial approval reads cleanly."""
    steps, issues, pairs = [], [], []
    for v in verdicts:
        (ta, ra), (tb, rb) = v["a"], v["b"]
        pairs.append([ta.block_id, tb.block_id])
        if v["verdict"] == "duplicate":
            keep_a = v["keep"] == ta.block_id
            win, wrel = (ta, ra) if keep_a else (tb, rb)
            lose, lrel = (tb, rb) if keep_a else (ta, ra)
            issues.append(f"duplicate: “{lose.text}” ({lrel}) ≡ kept in {wrel}")
            steps.append({"tool": "replace_line", "args": {
                "path": lrel, "line_text": lose.raw, "content": ""}})
        else:  # related — the pair's first task carries the ⇄ link
            src, srel, dst, drel = ta, ra, tb, rb
            issues.append(f"related: “{src.text}” ({srel}) ⇄ “{dst.text}” ({drel})")
            steps.append({"tool": "replace_line", "args": {
                "path": srel, "line_text": src.raw,
                "content": _linked_line(src.raw, drel, dst.block_id)}})
    return steps, issues, pairs


async def draft_overlap(w: Watcher, item: dict) -> dict | None:
    """Exact pairs with a one-sided metadata story resolve in code; every
    other candidate goes to the judge in ONE batched call. Verdicts the
    judge returns as distinct simply don't become steps — and the pool
    hash plus the queue's pair memory keep them from being re-asked."""
    verdicts: list[dict] = []
    to_judge: list[tuple] = []
    for a, b in item["exact"]:
        (ta, _ra), (tb, _rb) = a, b
        sa, sb = _metadata_score(ta), _metadata_score(tb)
        if sa and sb:
            to_judge.append((a, b))  # both carry metadata: not mechanically mergeable
            continue
        if sa != sb:
            win, lose = (a, b) if sa > sb else (b, a)
        else:  # both bare: the older line wins; tie falls to the first
            win, lose = (a, b) if (a[0].created or "") <= (b[0].created or "") else (b, a)
        verdicts.append({"a": win, "b": lose, "verdict": "duplicate",
                         "keep": win[0].block_id})
    fuzzy = await _fuzzy_candidates(w, item)  # None: embedder down
    to_judge += fuzzy or []
    if to_judge:
        # The judge call comes BEFORE the seen-hash stamp: a worker that
        # raises here leaves the pool armed, so it really is retried next
        # pass — which is what the surfaces say about a down worker.
        reply = await w.overlap_drafter(item, to_judge)
        # Answered — even a broken reply must not re-ask every pass (the
        # distinct verdicts below have no other memory than this stamp).
        w.seen["overlap"] = item["seen_key"]
        answered = {}
        for p in reply.get("pairs") or []:
            answered[frozenset((str(p.get("a")), str(p.get("b"))))] = p
        for a, b in to_judge:
            key = frozenset((a[0].block_id, b[0].block_id))
            p = answered.get(key)
            if p is None:
                return None  # silently-missing pairs are failures (grouping's rule)
            verdict = p.get("verdict")
            if verdict == "duplicate":
                keep = str(p.get("keep") or "")
                if keep not in key:
                    return None  # invented keep id: broken output dies here
                verdicts.append({"a": a, "b": b, "verdict": "duplicate", "keep": keep})
            elif verdict == "related":
                verdicts.append({"a": a, "b": b, "verdict": "related", "keep": None})
            elif verdict != "distinct":
                return None
    elif fuzzy is not None:
        # Nothing needed judging and the embedder answered: the pool is
        # settled. Embedder down with nothing to judge stays UNSTAMPED —
        # the retry costs one cheap scan, and the fuzzy lane recovers the
        # moment the endpoint does instead of waiting for the pool to
        # happen to change. (The already-drafted exact pairs can't
        # re-propose meanwhile: _decided_pairs counts pending ones.)
        w.seen["overlap"] = item["seen_key"]
    actionable = [v for v in verdicts if v["verdict"] in ("duplicate", "related")]
    if not actionable:
        return None
    steps, issues, pairs = _overlap_steps(actionable)
    p = proposals.new_proposal(
        kind="overlap",
        note=item["note"],
        vault_sha=item["vault_sha"],
        note_content=item["content"],
        title=f"Resolve {len(steps)} overlapping task pair(s)",
        summary="Duplicates: the redundant copy is removed (the kept line is "
        "untouched, and the removal is one revertible snapshot). Related: a "
        "⇄ link is added — links, not prose. Approve per pair.",
        issues=issues,
        apply=steps,
        entity_ids=[i for pair in pairs for i in pair],
    )
    p["pairs"] = pairs  # the queue's dismissal memory (see _decided_pairs)
    return p


# ---- plan_today: the judgment lane — pick today's work from the backlog ----

MAX_PLAN_PICKS = 3


def scan_plan(w: Watcher) -> list[dict]:
    """The undated pool: every open, top-level, stamped task carrying
    neither 📅 nor ⏳ nor 👤, wherever it lives (the same pool the
    dashboard's backlog reads). Nothing deterministic can place these,
    so placing them IS judgment, and judgment always queues (no
    verifier can vouch for a plan). One work item for the whole pool;
    the once-a-day cadence rides the seen-hash (keyed on date + pool),
    so a rejected plan never re-asks until tomorrow or the pool
    changes."""
    pool: list[tuple] = []  # (Task, rel)
    for rel, text in w.pass_notes().items():
        for t in tk.iter_tasks(text):
            if (not t.done and t.raw.lstrip() == t.raw and not t.due
                    and not t.scheduled and not t.owner and t.block_id):
                pool.append((t, rel))
    if not pool:
        return []
    # Oldest first — the order the judge reads, and the anchor note is
    # the oldest task's home.
    pool.sort(key=lambda x: (x[0].created or "9999", x[1], x[0].block_id))
    today = dt.date.today().isoformat()
    key = content_hash(today + "\n".join(t.block_id for t, _rel in pool))
    if key == w.seen.get("plan_today"):
        return []
    # What's already committed today (due or planned) — the worker needs
    # this to know when picking nothing is the right answer. Counted off
    # the pass's note cache: no extra walk.
    committed = 0
    for text in w.pass_notes().values():
        for t in tk.iter_tasks(text):
            if t.done:
                continue
            if (t.due and t.due <= today) or (t.scheduled and t.scheduled <= today):
                committed += 1
    anchor = pool[0][1]
    return [{
        "kind": "plan_today",
        "note": anchor,
        "content": w.pass_notes().get(anchor, ""),
        "vault_sha": w._pass_sha,
        "tasks": pool,
        "today": today,
        "committed": committed,
        "seen_key": key,
    }]


async def draft_plan(w: Watcher, item: dict) -> dict | None:
    reply = await w.plan_drafter(item)
    # Asked and answered for this pool+day, whatever the answer: an
    # empty or broken reply must not re-ask every pass.
    w.seen["plan_today"] = item["seen_key"]
    pool = {t.block_id: (t, rel) for t, rel in item["tasks"]}
    picks = reply.get("picks") or []
    ids = [str(p.get("id", "")) for p in picks]
    if not ids:
        return None  # "nothing worth surfacing today" is a correct answer
    if (len(ids) > MAX_PLAN_PICKS or len(set(ids)) != len(ids)
            or any(i not in pool for i in ids)):
        return None  # broken output dies here, never in the queue
    issues = []
    for p in picks:
        t, rel = pool[str(p["id"])]
        reason = str(p.get("reason", "")).strip()
        issues.append(t.text + (f" — {reason}" if reason else ""))
    return proposals.new_proposal(
        kind="plan_today",
        note=item["note"],
        vault_sha=item["vault_sha"],
        note_content=item["content"],
        title=f"Plan today: {len(ids)} task(s) from the backlog",
        summary="Approving stamps ⏳ today on the picked tasks — a plan, "
        "never a deadline. Approve some, reject the rest: every verdict "
        "teaches the judge your judgment.",
        issues=issues,
        # Per-step paths: the pool spans notes now, and set_task_scheduled
        # is its own staleness check (matches by the task's text, fails
        # loud when the line moved).
        apply=[{"tool": "set_task_scheduled", "args": {
            "path": pool[i][1], "task_text": pool[i][0].text,
            "scheduled": item["today"],
        }} for i in ids],
        entity_ids=ids,
    )


# ---- the registry ----

CAPTURE = ProcessorSpec(
    "capture", scan_capture, draft_capture,
    render_diff=False, ensure=ensure_capture, on_applied=_capture_applied,
    verify=verify_record,
    title="Capture dumps",
    blurb="Turns whatever you dump into the capture surface into a record: "
          "a meeting note or a dated capture note holding a readable "
          "rewrite, the tasks it contains (yours, and what others owe you "
          "marked 👤), and your verbatim dump folded beneath. A bare task "
          "list files itself once verified; a dump that earned a summary "
          "asks first — unless Spec.md grants autonomy: full, and then it "
          "files with the objection on the audit trail.",
)
SPEC_DRIFT = ProcessorSpec(
    "spec_drift", scan_spec_drift, draft_spec_drift,
    title="Unmapped folders",
    blurb="Notices a new top-level folder your vault spec doesn't know and "
          "proposes what note type belongs in it. Until a folder is mapped, "
          "notes inside it have no default type.",
)
SCHEMA_FIX = ProcessorSpec(
    "schema_fix", scan_schema, draft_schema, verify=verify_schema_fix,
    title="Entity schema fixes",
    blurb="Repairs notes whose frontmatter doesn't match the schema for "
          "their type — a missing or malformed id, a required field left "
          "out. Touches frontmatter only, never your prose.",
)
GROUPING = ProcessorSpec(
    "grouping", scan_grouping, draft_grouping,
    title="Group flat task lists",
    blurb="Suggests headings for a long unstructured task list. A judgment "
          "call with no deterministic check behind it, so it always asks.",
)
OVERLAP = ProcessorSpec(
    "overlap", scan_overlap, draft_overlap, render_diff=False,
    claims_note=False,
    title="Duplicate tasks",
    blurb="Finds the same task written twice in different notes and "
          "proposes merging the pair. Exact repeats are matched in code; "
          "near-misses are judged by the worker model, so it always asks.",
)
PLAN_TODAY = ProcessorSpec(
    "plan_today", scan_plan, draft_plan, render_diff=False,
    claims_note=False,
    title="Suggest a day plan",
    blurb="Once a day, picks a few backlog tasks carrying neither a deadline "
          "nor a plan date — wherever they live — and proposes doing them "
          "today. Nothing deterministic can place a task, so this always asks.",
)

# Capture first: the user actively feeding the pipeline is never starved
# out of n_max by a maintenance backlog. spec_drift and schema_fix come
# before grouping — the one-proposal-per-note invariant means whoever
# scans a note first claims it, and conformance (typing, ids) unlocks
# grouping anyway. overlap and plan_today are the cross-note lanes
# (claims_note=False): their pools span the vault and their anchors
# claim nothing, so they never contend with the per-note processors.
# Within the suggestion lanes: dedupe before planning (planning a task
# that's about to be merged away is wasted judgment). (//commands are
# not processors — they execute in their own pass phase; see
# commands.py.)
REGISTRY: tuple[ProcessorSpec, ...] = (
    CAPTURE, SPEC_DRIFT, SCHEMA_FIX, OVERLAP, PLAN_TODAY, GROUPING)


def registry(disabled: Sequence[str] = ()) -> tuple[ProcessorSpec, ...]:
    """The default processor set minus `disabled` names (the generalized
    rollback flag). Unknown names fail loud — a typo must not silently
    leave a processor running."""
    unknown = set(disabled) - {p.name for p in REGISTRY}
    if unknown:
        raise ValueError(f"unknown processor(s): {sorted(unknown)}")
    return tuple(p for p in REGISTRY if p.name not in disabled)
