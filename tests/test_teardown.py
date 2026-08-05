"""Process and undo lifecycle: the guarantees that only break at runtime.

Everything here failed silently before, and none of it was reachable
from the existing suites, because each guarantee is about what happens
AFTER the happy path returns — a process that outlives its session, a
commit range that reaches past what the harness authored. They are
grouped because they share that shape: the observable is a side effect,
so each test asserts on the leftovers rather than the return value.
"""

import asyncio

import pytest

from chickenskratch import changes, pane, proposals, service
from chickenskratch.profiles import Profile
from chickenskratch_vault.vault import Vault

# ---- the pane's teardown: a stopped session must not leak its servers ----


class FakeAgent:
    """An Agent-shaped object whose turn parks until released — the
    in-flight turn a stop button has to unwind."""

    def __init__(self):
        self.profile = Profile(name="fake", model="m", request_timeout_s=1.0)
        self.messages = []
        self.exited = False
        self.release = asyncio.Event()

    async def run(self, text):
        await self.release.wait()
        return "done"

    async def __aexit__(self, *exc):
        self.exited = True


def _session(agent):
    async def factory():
        return agent
    return pane.ChatSession("/tmp/vault", agent_factory=factory)


def test_close_never_cancels_the_worker_that_owns_the_transports():
    """The whole finding, pinned: close() may give up WATCHING a slow
    teardown, but it must never cancel the worker task. That task is the
    only one that can exit the MCP transports' cancel scopes — cancelling
    it strands them, and the stdio servers the session spawned outlive it
    for as long as the process runs."""
    async def scenario():
        agent = FakeAgent()
        s = _session(agent)
        turn = asyncio.create_task(s.send("hello"))
        await asyncio.sleep(0.05)          # let the worker pick the turn up
        worker = s._worker
        assert worker is not None and not worker.done()

        s.stop()
        await s.close(timeout=0.1)         # far shorter than the parked turn
        assert not worker.cancelled(), "close() cancelled the worker"
        assert not worker.done(), "close() ended the worker mid-turn"
        assert not agent.exited, "the agent was torn down by the wrong task"

        # The turn finishes on its own, and THEN the session's own worker
        # runs the teardown it was handed — the servers close properly,
        # just later than the impatient caller waited.
        agent.release.set()
        await asyncio.wait_for(turn, 2)
        await asyncio.wait_for(worker, 2)
        assert agent.exited, "the deferred teardown never ran"

    asyncio.run(scenario())


def test_close_tears_down_promptly_when_the_turn_is_idle():
    """The ordinary case still resolves fast: no in-flight turn means the
    sentinel is picked up immediately and the agent exits."""
    async def scenario():
        agent = FakeAgent()
        agent.release.set()
        s = _session(agent)
        await asyncio.wait_for(s.send("hello"), 2)
        await asyncio.wait_for(s.close(), 2)
        assert agent.exited
        assert s._worker is None

    asyncio.run(scenario())


def test_a_closed_session_accepts_no_further_turns():
    """close() marks the session stopped. Without that, a send() landing
    after teardown would spin up a SECOND worker beside the one that
    still owns the agent's transports."""
    async def scenario():
        agent = FakeAgent()
        agent.release.set()
        s = _session(agent)
        await asyncio.wait_for(s.close(), 2)
        await asyncio.wait_for(s.send("late"), 2)
        assert s._worker is None, "a closed session started a new worker"

    asyncio.run(scenario())


def test_teardown_budget_is_derived_from_the_turn_it_waits_on():
    """Not a magic number: the budget must cover one in-flight model call
    plus one in-flight tool call, the two checkpoints a stopped turn can
    be parked on. A budget shorter than a single reply is what made the
    cancel path routine."""
    from chickenskratch.mcp_client import CALL_TIMEOUT

    slow = Profile(name="slow", model="m", request_timeout_s=300.0)
    assert pane.teardown_budget(slow) >= 300.0 + CALL_TIMEOUT
    # No profile yet (the agent never built): still bounded, still >= the
    # default request timeout rather than falling back to zero.
    assert pane.teardown_budget(None) >= Profile.request_timeout_s


# ---- undo: only the harness's own commits are the harness's to revert ----


def _approve(vault, make_watcher, proposal):
    proposals.save(proposal, make_watcher(vault).proposals_dir)
    return asyncio.run(service.decide(
        str(vault), proposal["id"], "approve",
        proposals_dir=make_watcher(vault).proposals_dir,
        state_path=make_watcher(vault).state_path))


def _capture_proposal(vault):
    note = "Notes/undo.md"
    (vault / "Notes").mkdir(exist_ok=True)
    (vault / "Notes/undo.md").write_text("# Undo\n\n## Log\n\nold\n")
    Vault(vault).commit_if_dirty("seed the target note")
    return proposals.new_proposal(
        kind="schema_fix", note=note,
        vault_sha=Vault(vault).history(1)[0]["sha"],
        note_content=(vault / "Notes/undo.md").read_text(),
        title="rewrite the log", summary="", issues=[],
        apply=[{"tool": "replace_section",
                "args": {"path": note, "heading": "Log", "content": "new"}}],
    )


def test_undo_excludes_the_users_own_hand_edits(gitvault, make_watcher):
    """The data-loss case. A mutation snapshots the tree first, so any
    uncommitted writing the user had in flight lands in a `manual edits`
    commit right beside the change. Undo must revert the harness's commit
    and leave theirs alone — reverting the range takes their work with
    it."""
    p = _capture_proposal(gitvault)
    # the user is mid-edit on an unrelated note when they hit approve
    (gitvault / "Notes/mine.md").write_text("# Mine\n\nwords I just typed\n")

    summary = _approve(gitvault, make_watcher, p)
    assert summary["approved"] == 1

    v = Vault(gitvault)
    subjects = [v.commit_message(sha).splitlines()[0] for sha in summary["undo"]]
    assert subjects, "an approve reported no undo at all"
    assert all(s.startswith("harness:") for s in subjects), subjects
    assert not any("manual edits" in s for s in subjects), subjects

    for sha in summary["undo"]:
        ok, err = v.revert(sha)
        assert ok, err
    assert (gitvault / "Notes/mine.md").read_text() == "# Mine\n\nwords I just typed\n"


def test_undo_range_stops_at_the_harness_commits_of_this_apply(gitvault, make_watcher):
    """The range is bracketed around THIS plan's writes, not derived from
    a HEAD read before a lock wait — so an earlier harness commit that
    happens to sit in the window is not swept in."""
    v = Vault(gitvault)
    (gitvault / "Notes").mkdir(exist_ok=True)
    (gitvault / "Notes/earlier.md").write_text("earlier\n")
    v.commit_if_dirty("harness: an unrelated earlier action")
    earlier = v.history(1)[0]["sha"]

    p = _capture_proposal(gitvault)
    summary = _approve(gitvault, make_watcher, p)
    assert earlier not in summary["undo"]


def test_harness_commits_since_is_the_one_derivation(gitvault):
    """Both consent paths call this; it is the only place the rule
    lives. Pinned directly so a future caller can't quietly reimplement
    the unsafe version."""
    v = Vault(gitvault)
    base = v.history(1)[0]["sha"]
    (gitvault / "a.md").write_text("a\n")
    v.commit_if_dirty("manual edits (before harness: replace_section)")
    (gitvault / "b.md").write_text("b\n")
    v.commit_if_dirty("harness: replace_section in a.md")

    shas = changes.harness_commits_since(v, base)
    assert [v.commit_message(s).splitlines()[0] for s in shas] == [
        "harness: replace_section in a.md"]


# ---- the capture surface: a failed clear must not become a duplicate ----


def test_a_failed_surface_clear_is_finished_on_the_next_pass(vault, make_watcher):
    """reset_capture can fail on a wedged vault lock. The record already
    landed, so the dump left on the surface is a duplicate waiting for
    the user's next keystroke to move the seen-hash. The flag records
    exactly what was filed; the next pass finishes the clear."""
    from chickenskratch import processors
    from chickenskratch.capture import CAPTURE_SKELETON, capture_text
    from chickenskratch_vault.vault import VaultError

    w = make_watcher(vault)
    processors.ensure_capture(w)
    w.write_surface(w.capture_rel,
                    CAPTURE_SKELETON + "\nbuy milk\n", "dump", action="ingest")
    filed = processors.content_hash(capture_text(w.vault.read(w.capture_rel)))

    def wedged(_w):
        raise VaultError("vault lock held by another process")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(processors, "reset_capture", wedged)
        with pytest.raises(VaultError):
            processors._capture_applied(w, {"kind": "capture"})
    assert w.seen[processors.PENDING_RESET] == filed
    assert "buy milk" in w.vault.read(w.capture_rel), "the dump vanished"

    processors.ensure_capture(w)   # the next pass's start hook
    assert processors.PENDING_RESET not in w.seen
    assert "buy milk" not in w.vault.read(w.capture_rel), "the clear never completed"


def test_a_deferred_clear_never_eats_text_typed_since(vault, make_watcher):
    """The one branch where the clear must NOT run. Overwriting the whole
    surface would delete words the user typed after the failure, which is
    worse than the duplicate it prevents — so the flag is dropped, the
    text stays, and the failure is announced instead."""
    from chickenskratch import processors
    from chickenskratch.capture import CAPTURE_SKELETON

    w = make_watcher(vault)
    processors.ensure_capture(w)
    w.write_surface(w.capture_rel, CAPTURE_SKELETON + "\nbuy milk\n",
                    "dump", action="ingest")
    w.seen[processors.PENDING_RESET] = "a-hash-from-an-earlier-dump"

    processors.ensure_capture(w)
    assert processors.PENDING_RESET not in w.seen
    assert "buy milk" in w.vault.read(w.capture_rel), "the user's text was eaten"
