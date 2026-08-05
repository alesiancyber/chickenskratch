"""Blocking-release failure classes, pinned: one bad input — a malformed
queue file, a garbage env var, a mis-encoded note, a crashing scan — may
cost its own unit of work, never the engine pass. (The 2026-07-24 audit:
every wedge we ever hit was one of these.)"""

import asyncio
import json
from types import SimpleNamespace

from chickenskratch import proposals
from chickenskratch.processors import ProcessorSpec
from chickenskratch_vault import server, workers


def run(w):
    return asyncio.run(w.run_once())


def test_malformed_proposal_file_never_wedges_the_queue(gitvault, make_watcher):
    w = make_watcher(gitvault)
    w.proposals_dir.mkdir(parents=True, exist_ok=True)
    # valid JSON, wrong shape — the hand-edit/truncated-write case
    (w.proposals_dir / "p-000000000bad.json").write_text("{}", encoding="utf-8")
    assert proposals.pending(w.proposals_dir) == []  # skipped, not KeyError
    summary = run(w)
    assert not summary["locked"]  # the pass ran to completion


def test_poison_scan_fails_its_processor_not_the_pass(gitvault, make_watcher):
    def boom(w):
        raise RuntimeError("scan exploded")

    async def draft(w, item):
        return None

    w = make_watcher(gitvault, registry=(ProcessorSpec("boom", boom, draft),))
    summary = run(w)
    assert summary["scanned"] is True  # the stage survived its poison member


def _fake_client(content: str):
    async def create(**kwargs):
        msg = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_garbage_worker_env_degrades_to_spec_defaults(monkeypatch):
    monkeypatch.setenv("WORKER_TIMEOUT_S", "60s")
    monkeypatch.setenv("WORKER_MAX_TOKENS", "lots")
    monkeypatch.setattr(workers, "_worker_client", lambda: _fake_client(json.dumps({"tasks": []})))
    # a ValueError here would escape run_worker's VaultError contract and
    # kill every engine pass over an .env typo
    tasks = asyncio.run(workers.extract_tasks("nothing to do", "2026-07-24"))
    assert tasks == []


def test_mis_encoded_note_never_crashes_walks_or_the_pass(gitvault, make_watcher):
    (gitvault / "Tasks" / "Bad.md").write_bytes(
        b"# ok\n\xff\xfe broken \x80 bytes\n- [ ] survives\n")
    assert server.list_tasks()["total"] >= 1        # tolerant read-only walk
    server.search_vault("survives")                 # tolerant read-only walk
    w = make_watcher(gitvault, registry=())
    summary = run(w)                                # Today render + command scan survive
    assert not summary["locked"]


def test_untracked_note_edits_move_the_pass_fingerprint(gitvault, make_watcher):
    """A brand-new note is untracked, and `git status --porcelain` says
    `?? Tasks/typing.md` no matter what the file contains. So editing it
    left the pass fingerprint unchanged, the engine skipped the scan, and
    a //command typed there never fired.

    This was invisible until 2026-07-26: the engine re-rendered its
    markdown surfaces every pass, and those commits swept untracked notes
    into git as a side effect. Retiring the surfaces removed the
    accidental heartbeat — so the fingerprint has to see content itself."""
    from chickenskratch import applier

    note = gitvault / "Tasks/brand-new.md"
    note.write_text("# New\n\nfirst thought\n")
    first = applier.vault_state(str(gitvault))
    note.write_text("# New\n\nsecond thought\n")
    second = applier.vault_state(str(gitvault))
    assert first != second, "an untracked note's content must reach the fingerprint"

    # and a deleted untracked file doesn't crash the walk (I6: tolerant reads)
    note.unlink()
    assert applier.vault_state(str(gitvault)) != second


# ---- broken worker output must be budgeted, not looped ----
#
# The failure this pins, measured in a live vault: schema_fix drafted
# against ONE unchanged note 21 times, every pass reporting
# `drafted=0 auto=0 worker_errors=[]` while a 16-to-631-second model call
# ran behind it. The drafter was declining with `return None`, the engine
# read that as "nothing to propose" and CLEARED the retry budget, and the
# deterministic scan re-found the same note on the next pass. Forever.

# every lane but schema_fix, so a pass drafts exactly the one item below
ONLY_SCHEMA = ("capture", "spec_drift", "overlap", "plan_today", "grouping")
BROKEN_REL = "Tasks/Broken.md"


def one_broken_note(vault):
    """A vault whose ONLY nonconforming note is ours. The fixture vault
    ships several, and n_max would draft five per pass — which measures
    the budget of whichever note sorted first, not of this one."""
    for md in vault.rglob("*.md"):
        if md.name != "Spec.md":
            md.write_text("---\ntype: note\nid: n-000000000000\n---\n\n# ok\n")
    (vault / "Tasks").mkdir(exist_ok=True)
    (vault / BROKEN_REL).write_text(
        "---\ntype: task\n---\n\n# Broken\n\n- [ ] no id on this line\n")
    return BROKEN_REL


def wrong_note_drafter(calls):
    """A worker that answers, but aims its plan at another note — the
    exact shape the live model produced 21 times."""
    async def draft(item):
        calls.append(item["note"])
        return {"title": "t", "summary": "s",
                "apply": [{"tool": "update_frontmatter",
                           "args": {"path": "Some/Other.md", "key": "id",
                                    "value": "n-1"}}]}
    return draft


def test_a_worker_plan_aimed_elsewhere_is_budgeted_then_parked(vault, make_watcher):
    """A plan whose steps target a different note can never become a
    proposal. It must cost a retry budget and be SAID OUT LOUD — a lane
    that re-finds its items by scan keeps no seen-hash for a parking to
    ride on, so a silent decline is one model call per pass, forever."""
    from chickenskratch import watcher as watcher_mod

    one_broken_note(vault)
    calls = []
    for attempt in range(1, watcher_mod.DRAFT_PLAN_ATTEMPTS + 1):
        w = make_watcher(vault, drafter=wrong_note_drafter(calls),
                         disabled=ONLY_SCHEMA)
        summary = run(w)
        assert summary["drafted"] == 0
        errs = [e for e in summary["worker_errors"] if e["processor"] == "schema_fix"]
        assert errs, "a broken plan reported nothing — a quiet vault looks the same"
        assert "targets" in errs[0]["error"]
        assert errs[0]["retrying"] is (attempt < watcher_mod.DRAFT_PLAN_ATTEMPTS)

    assert calls == [BROKEN_REL] * watcher_mod.DRAFT_PLAN_ATTEMPTS
    # parked: a further pass must not reach the model at all
    w = make_watcher(vault, drafter=wrong_note_drafter(calls), disabled=ONLY_SCHEMA)
    assert run(w)["worker_errors"] == []
    assert len(calls) == watcher_mod.DRAFT_PLAN_ATTEMPTS, (
        f"a parked lane still called the worker ({len(calls)} calls)")
    slots = json.loads(w.state_path.read_text())["draft_attempts"]
    assert any(s.startswith("schema_fix:") for s in slots)


def test_editing_the_note_buys_a_fresh_budget(vault, make_watcher):
    """Parking is per INPUT, not per note: the user changing the text is
    exactly the event that deserves another try."""
    from chickenskratch import watcher as watcher_mod

    rel = one_broken_note(vault)
    calls = []
    for _ in range(watcher_mod.DRAFT_PLAN_ATTEMPTS + 1):
        run(make_watcher(vault, drafter=wrong_note_drafter(calls),
                         disabled=ONLY_SCHEMA))
    assert len(calls) == watcher_mod.DRAFT_PLAN_ATTEMPTS

    (vault / rel).write_text(
        "---\ntype: task\n---\n\n# Broken\n\n- [ ] a different unstamped line\n")
    run(make_watcher(vault, drafter=wrong_note_drafter(calls), disabled=ONLY_SCHEMA))
    assert len(calls) == watcher_mod.DRAFT_PLAN_ATTEMPTS + 1, (
        "an edited note did not re-arm the lane")


def test_nothing_to_propose_stays_free(vault, make_watcher):
    """The other half of the split, and the reason it needed one: a
    correct "nothing here" must NOT burn budget, or a healthy quiet lane
    would eventually park itself."""
    from chickenskratch import watcher as watcher_mod

    one_broken_note(vault)

    async def usable(item):
        return {"title": "t", "summary": "s", "apply": [
            {"tool": "update_frontmatter",
             "args": {"path": item["note"], "key": "id", "value": "n-1"}}]}

    for _ in range(watcher_mod.DRAFT_PLAN_ATTEMPTS + 2):
        w = make_watcher(vault, drafter=usable, disabled=ONLY_SCHEMA)
        assert [e for e in run(w)["worker_errors"]
                if e["processor"] == "schema_fix"] == []
    slots = json.loads(w.state_path.read_text()).get("draft_attempts") or {}
    assert not [s for s in slots if s.startswith("schema_fix:")], (
        "a healthy lane accumulated a retry budget")


# ---- one absurd file must cost itself, not the vault ----


def test_an_oversize_note_is_refused_by_the_strict_read(vault):
    """Vault.read is where every mutation starts, so it refuses rather
    than truncates: a read-modify-write over a partial view would delete
    everything past the cut, and that is worse than any error."""
    from chickenskratch_vault.vault import MAX_NOTE_BYTES, Vault, VaultError

    (vault / "Huge.md").write_text("# Huge\n\n" + "x" * (MAX_NOTE_BYTES + 1000))
    try:
        Vault(str(vault)).read("Huge.md")
    except VaultError as e:
        assert "ceiling" in str(e) and "MB" in str(e)
    else:
        raise AssertionError("an oversize note was read whole")


def test_an_oversize_note_is_truncated_by_the_scan_read(vault):
    """The lossy reader truncates instead: a walk that silently dropped
    the file would report "nothing here" about a note plainly on disk.
    Safe only because the strict read above refuses it — no partial view
    can become a partial write."""
    from chickenskratch_vault.vault import MAX_NOTE_BYTES, read_lossy

    p = vault / "Huge.md"
    p.write_text("# Huge\n\n" + "x" * (MAX_NOTE_BYTES * 2))
    text = read_lossy(p)
    assert len(text.encode()) <= MAX_NOTE_BYTES
    assert text.startswith("# Huge")


def test_an_oversize_note_does_not_wedge_a_pass(vault, make_watcher):
    """The whole point: a log file someone renamed .md lands in the vault
    and the engine keeps working."""
    from chickenskratch_vault.vault import MAX_NOTE_BYTES

    (vault / "Notes").mkdir(exist_ok=True)
    (vault / "Notes/dump.md").write_text(
        "---\ntype: note\n---\n\n" + "- [ ] a task with no id\n" * 40000
        + "y" * MAX_NOTE_BYTES)
    summary = run(make_watcher(vault, disabled=ONLY_SCHEMA))
    assert summary["scanned"] and not summary["locked"]


def test_a_normal_note_is_untouched_by_the_ceiling(vault):
    """The bound is for what arrives from outside — it must be nowhere
    near anything a person writes."""
    from chickenskratch_vault.vault import MAX_NOTE_BYTES, Vault, read_lossy

    body = "# Journal\n\n" + ("a paragraph of perfectly ordinary prose. " * 40 + "\n") * 300
    assert len(body.encode()) < MAX_NOTE_BYTES // 4  # a half-year journal, comfortably
    (vault / "Journal.md").write_text(body)
    assert Vault(str(vault)).read("Journal.md") == body
    assert read_lossy(vault / "Journal.md") == body
