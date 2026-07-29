"""Human-verb service entries: decide / ingest / revert / run_pass.

The convergence point for every consent channel: the dashboard's
buttons and `tezcat decide` meet at the same engine pass under the
same flock pass lock, so two front doors can never race or
double-apply, and nothing bypasses a gate (I1/I2). Reads have no entry
here — views are free (I5); these are the write verbs, all
human-initiated: models have no route to any of them.
"""

from __future__ import annotations

import re

from tezcat_vault.vault import Vault, VaultError
from tezcat_vault.vaultconfig import get_config

from . import cancel, commands, events, processors, proposals
from .corpus import record_reschedule, record_revert
from .watcher import DECISION_LOCK_WAIT_S, LockBusy, Watcher

_SHA_RE = re.compile(r"[0-9a-f]{7,40}")
_PID_RE = re.compile(r"p-[0-9a-f]{12}")


async def run_pass(vault_path: str, decisions_only: bool = False, **watcher_kw) -> dict:
    """One engine pass, exactly as the daemon and CLI trigger it.
    `watcher_kw` are Watcher injection points (tests: isolated
    queue/state) — same convention on every entry below.

    Armed for the stop button: the flag is cleared before starting (a
    stale request must never kill the pass the user just asked for) and
    the token is handed to the engine, which polls it between drafts."""
    cancel.clear(cancel.PASS)
    watcher_kw.setdefault("should_cancel", cancel.token(cancel.PASS))
    return await Watcher(vault_path, **watcher_kw).run_once(decisions_only=decisions_only)


async def decide(vault_path: str, pid: str, verdict: str,
                 steps: list[int] | None = None, **watcher_kw) -> dict:
    """Act on one proposal — the UI button's and `tezcat decide`'s path.
    Runs the decisions fast lane; every channel resolves through
    Watcher._act under the pass lock.

    Waits for the pass lock (DECISION_LOCK_WAIT_S) rather than skipping
    on contention: the verdict lives only in this call, so a `locked`
    return would silently throw away a human decision while the
    dashboard optimistically rendered it as done. On a real timeout this
    raises LockBusy and the route says so.

    `steps` (approve only) is partial approval: the queue file is
    trimmed to the chosen apply steps and the trimmed proposal is what
    applies, goes stale-checked, and lands in the corpus (the queue file
    IS the consent record). An empty selection is a reject.
    Single-user assumption: the trim and the pass are not atomic
    against a concurrent engine pass."""
    if verdict not in ("approve", "reject"):
        raise ValueError(f"verdict must be approve|reject, got {verdict!r}")
    if not _PID_RE.fullmatch(pid):
        raise ValueError(f"not a proposal id: {pid!r}")
    w = Watcher(vault_path, **watcher_kw)
    untrimmed = None
    if steps is not None and verdict == "approve":
        p = next((x for x in proposals.pending(w.proposals_dir) if x["id"] == pid), None)
        if p is None:
            raise ValueError(f"unknown or already-resolved proposal: {pid}")
        keep = sorted({int(s) for s in steps})
        if any(s < 0 or s >= len(p["apply"]) for s in keep):
            raise ValueError("step index out of range")
        if not keep:
            verdict = "reject"
        elif len(keep) < len(p["apply"]):
            untrimmed = p
            # `partial` rides in the consent record: on_applied hooks must
            # know (a partial capture approval keeps the dump on the
            # surface — clearing it would discard the text behind the
            # steps the user did NOT approve).
            proposals.save({**p, "apply": [p["apply"][i] for i in keep],
                            "partial": True},
                           w.proposals_dir)
    try:
        return await w.run_once(decisions_only=True, decisions=[(pid, verdict)],
                                lock_wait=DECISION_LOCK_WAIT_S)
    except LockBusy:
        # The decision didn't happen, so its trim must not survive either:
        # the queue file is the consent record, and leaving it trimmed
        # would silently redefine the proposal the user retries on.
        if untrimmed is not None:
            proposals.save(untrimmed, w.proposals_dir)
        raise


def complete_task(vault_path: str, note: str, text: str) -> dict:
    """Check a task off from the dashboard. The editor always had this
    hand (ticking the checkbox in the note); this gives the dashboard
    the same one, through the gated set_task_status tool: snapshot per
    mutation (I1), matched by the task's own text. Raises VaultError
    when nothing matches (the note moved — the row was stale, refetch)."""
    from . import applier
    applier.use_vault(vault_path)
    from tezcat_vault import server

    result = server.set_task_status(note, text, True)
    events.emit(vault_path, "vault_changed", {"reason": "task_done", "note": note})
    return result


def reschedule_task(vault_path: str, note: str, text: str, due: str) -> dict:
    """Move a task along the time axis — the week view's "→ today", and
    the same shape for any future defer verb. A human verb on the gated
    set_task_due tool: snapshot per mutation (I1), matched by the task's
    own text, raises VaultError when nothing matches (stale row —
    refetch). Every reschedule lands in the corpus with its from/to
    dates: which tasks a human pulls forward, and which they push, is
    exactly what a future triage worker trains on."""
    from . import applier
    applier.use_vault(vault_path)
    from tezcat_vault import server

    result = server.set_task_due(note, text, due)
    if result.get("written"):
        record_reschedule(note, result["line"], result["task"],
                          result.get("previous_due"), result.get("due"),
                          field="due")
        events.emit(vault_path, "vault_changed",
                    {"reason": "reschedule", "note": note})
    return result


def plan_task(vault_path: str, note: str, text: str, when: str) -> dict:
    """Promote or defer a task's ⏳ plan date — the judgment verb. Most
    tasks carry no hard 📅, so "work on this today" / "push this out"
    moves ⏳ only; a real deadline is never fabricated or touched.
    Same contract as reschedule_task: gated tool, snapshot per mutation
    (I1), corpus row per move — the promote/defer stream is the training
    data a future triage worker learns this user's judgment from."""
    from . import applier
    applier.use_vault(vault_path)
    from tezcat_vault import server

    result = server.set_task_scheduled(note, text, when)
    if result.get("written"):
        record_reschedule(note, result["line"], result["task"],
                          result.get("previous_scheduled"),
                          result.get("scheduled"), field="scheduled")
        events.emit(vault_path, "vault_changed",
                    {"reason": "reschedule", "note": note})
    return result


async def ingest(vault_path: str, text: str, **watcher_kw) -> dict:
    """One capture entry: text in, a record out — auto-filed when
    verify_record can vouch for everything (a bare task list), queued
    when it carries a rewrite (or always, under auto_apply: []). The
    canvas posts here; typing into Capture.md feeds the same pipeline
    through the scan — two inputs, one pipeline. The dump lands on the
    capture surface through the gated surface write (snapshot-bracketed,
    so it is revertible and survives even if decomposition fails), then
    a full pass runs now instead of at the next trigger. Returns the
    pass summary plus what the dump queued and auto-filed."""
    text = text.strip()
    w = Watcher(vault_path, **watcher_kw)
    if not text:
        return {"summary": {}, "queued": [], "applied": [], "deferred": False}
    if not w.capture_rel:
        raise ValueError("this vault has no capture surface configured")
    processors.ensure_capture(w)
    try:
        raw = w.vault.read(w.capture_rel)
    except VaultError:
        raw = processors.CAPTURE_SKELETON
    w.write_surface(w.capture_rel, raw.rstrip("\n") + "\n\n" + text + "\n",
                    f"ingest into {w.capture_rel}", action="ingest")
    before = {p["id"] for p in proposals.load_all(w.proposals_dir)}
    try:
        summary = await w.run_once(lock_wait=DECISION_LOCK_WAIT_S)
    except LockBusy:
        # Unlike a decision, the dump is already durable on the capture
        # surface (snapshot-bracketed above) — the next pass extracts it.
        # So this is deferral, not failure, and the canvas says so.
        return {"summary": {"locked": True}, "queued": [], "applied": [],
                "deferred": True}
    fresh = [p for p in proposals.load_all(w.proposals_dir) if p["id"] not in before]
    queued = [p for p in fresh if p["status"] == "pending"]
    # What this dump auto-applied, WITH its undo — the canvas puts a
    # revert button on the receipt, so the point of action is also the
    # point of undo (a canvas revert is the same negative label as any
    # other).
    applied = [{
        "id": p["id"], "kind": p["kind"], "title": p["title"],
        "note": (p["apply"][0].get("args", {}).get("path")
                 if p.get("apply") else "") or p["drafted_against"]["note"],
        "undo": p.get("undo", []),
        "unvouched": bool(p.get("verifier_failures")),
    } for p in fresh if p["status"] == "auto"]
    # A worker that never answered (down, rate-limited) drafts nothing —
    # exactly like a dump with nothing in it. Say which happened: the text
    # is already durable on the capture surface and re-arms, so this is a
    # retry, not a loss, and "nothing extractable" would be a lie.
    return {"summary": summary, "queued": queued, "applied": applied,
            "deferred": False,
            "worker_errors": summary.get("worker_errors", [])}


def read_note(vault_path: str, rel: str) -> dict:
    """One note's raw markdown, byte-exact, for the owned editor. No
    parsing, no normalization: the editor's document IS these bytes, and
    `hash` is the token it must hand back to write them (see write_note).
    Vault.resolve enforces the exclusions, so Private/ is unreadable here
    exactly as it is everywhere else (I4)."""
    v = Vault(vault_path, exclude=get_config(vault_path).exclude)
    text = v.read(rel)
    return {"path": rel, "text": text, "hash": proposals.content_hash(text)}


def write_note(vault_path: str, rel: str, text: str, base_hash: str) -> dict:
    """Save an edited note — the human channel, so no _writable gate (that
    one exists to stop MODELS writing the consent surfaces, I2) but every
    other guarantee holds: locked, snapshot-bracketed, revertible.

    `base_hash` is the same staleness idea the proposal queue uses, turned
    around: the engine auto-applies while you type, so a save whose base
    no longer matches disk would silently eat that change. Mismatch raises
    RuntimeError and the editor re-reads instead of clobbering."""
    v = Vault(vault_path, exclude=get_config(vault_path).exclude)
    machine = set(get_config(vault_path).surfaces.values())
    with v.locked():
        current = v.read(rel)
        if base_hash and proposals.content_hash(current) != base_hash:
            raise RuntimeError(
                f"{rel} changed on disk since you opened it — reload before saving")
        if current == text:
            return {"path": rel, "hash": base_hash, "written": False}
        v.snapshot_before("edit_note")
        v.write(rel, text)
        v.snapshot_after(f"edit {rel}")
    events.emit(vault_path, "vault_changed", {"reason": "edit_note", "note": rel})
    return {
        "path": rel,
        "hash": proposals.content_hash(text),
        "written": True,
        # Machine-managed surfaces (Capture.md) can be rewritten by the
        # engine; saying so beats letting the user discover it when
        # their edit vanishes.
        "ephemeral": rel in machine,
    }


async def run_command(vault_path: str, name: str, args: str) -> str | None:
    """Execute one typed //command — the canvas equivalent of the
    in-note order (I3: typing the command IS the consent; trust is
    intrinsic, nothing queues). The result returns to the canvas and
    the vault is untouched: an in-note command edits its own line, a
    canvas command has no line. None = transient failure, same contract
    as the pass's command phase (the user just retries). Honors the
    Spec.md `inline_command` kill switch like the pass does."""
    if "inline_command" in get_config(vault_path).disabled_processors:
        raise ValueError("commands are disabled in this vault's Spec.md")
    fn = commands.INLINE_COMMANDS.get(name)
    if fn is None:
        raise ValueError(f"unknown command //{name}")
    return await fn(args, vault_path)


def revert(vault_path: str, sha: str) -> str:
    """Undo one snapshot as a new commit — the CLI's verb and the UI's
    undo. Returns the reverted commit's subject. Raises ValueError on a
    bad sha, RuntimeError when git refuses. Every revert is a human
    negative label, so it lands in the corpus (attributed via the
    commit's Chat-Id trailer when it carries one)."""
    sha = sha.strip().lower()
    if not _SHA_RE.fullmatch(sha):
        raise ValueError(f"not a commit sha: {sha!r}")
    v = Vault(vault_path)
    message = v.commit_message(sha)
    if not message.splitlines():
        raise ValueError(f"no such commit: {sha}")
    ok, error = v.revert(sha)
    if not ok:
        raise RuntimeError(f"revert failed: {error}")
    chat_match = re.search(r"^Chat-Id:\s*(\S+)", message, re.M)
    record_revert(chat_match.group(1) if chat_match else "", "", [],
                  sha, message.strip())  # swallows its own failures
    events.emit(vault_path, "vault_changed", {"reason": "revert", "sha": sha})
    return message.splitlines()[0]
