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
