"""Service entries (owned-surfaces 0-C): the UI's verbs converge on the
same engine pass as the checkbox scan — decide acts through _act under
the pass lock, ingest feeds the capture pipeline, and every completed
pass emits an event the SSE stream can tail."""

import asyncio

import pytest

from tezcat import events, proposals, service
from tezcat.watcher import Watcher
from tezcat_vault.vault import Vault, VaultError


@pytest.fixture()
def vault(gitvault):
    return gitvault


@pytest.fixture()
def wkw(tmp_path, monkeypatch):
    """Watcher injection kwargs: isolated queue/state, corpus off."""
    monkeypatch.setattr("tezcat.watcher.record_dashboard", lambda *a, **k: None)
    return {"proposals_dir": tmp_path / "proposals", "state_path": tmp_path / "state.json"}


def plant_proposal(vault, wkw) -> dict:
    p = proposals.new_proposal(
        kind="schema_fix", note="Home.md", vault_sha="",
        note_content=(vault / "Home.md").read_text(),
        title="Add a task", summary="planted", issues=["x"],
        apply=[{"tool": "add_task", "args": {"path": "Tasks/Inbox.md", "text": "planted task"}}],
    )
    proposals.save(p, wkw["proposals_dir"])
    return p


def test_decide_approve_applies_through_the_gated_path(vault, wkw):
    p = plant_proposal(vault, wkw)
    summary = asyncio.run(service.decide(str(vault), p["id"], "approve", **wkw))
    assert summary["approved"] == 1
    assert "planted task" in (vault / "Tasks/Inbox.md").read_text()
    assert proposals.pending(wkw["proposals_dir"]) == []


def test_decide_reject_resolves_without_touching_the_vault(vault, wkw):
    p = plant_proposal(vault, wkw)
    before = (vault / "Tasks/Inbox.md").read_text()
    summary = asyncio.run(service.decide(str(vault), p["id"], "reject", **wkw))
    assert summary["rejected"] == 1
    assert (vault / "Tasks/Inbox.md").read_text() == before


def test_decide_validates_its_inputs(vault, wkw):
    with pytest.raises(ValueError):
        asyncio.run(service.decide(str(vault), "p-abc", "approve", **wkw))
    with pytest.raises(ValueError):
        asyncio.run(service.decide(str(vault), "p-" + "a" * 12, "maybe", **wkw))


def test_the_same_verdict_twice_never_double_applies(vault, wkw):
    """Two clicks, a retried request, the dashboard and `tezcat decide`
    racing — the proposal resolves once. _act's resolved-set is the
    convergence guarantee, and it is now the ONLY thing standing between
    a double-submit and a doubled edit (2026-07-26: the checkbox channel
    that used to share it is gone, but the guarantee is unchanged)."""
    p = plant_proposal(vault, wkw)
    first = asyncio.run(service.decide(str(vault), p["id"], "approve", **wkw))
    second = asyncio.run(service.decide(str(vault), p["id"], "approve", **wkw))
    assert first["approved"] == 1 and second["approved"] == 0
    assert (vault / "Tasks/Inbox.md").read_text().count("planted task") == 1


def test_ingest_feeds_the_capture_pipeline(vault, wkw):
    """A canvas dump rides the exact editor pipeline: gated append to the
    capture surface, extraction, and — capture being auto-trusted by
    default with a passing fidelity verifier — auto-apply + surface
    reset. The dump itself survives in git (the ingest snapshot)."""
    from tezcat_vault.vault import Vault

    async def extractor(text, today):
        assert "renew the domain" in text
        return [{"text": "renew the domain", "due": "2026-08-01"}]

    result = asyncio.run(service.ingest(
        str(vault), "renew the domain by friday",
        capture_extractor=extractor,
        disabled=("inbox_extract", "spec_drift", "schema_fix", "grouping", "archive_done"),
        **wkw))
    assert result["summary"]["auto"] == 1
    assert "renew the domain" in (vault / "Tasks/Inbox.md").read_text()
    # applied -> the surface reset for the next dump; the dump text is
    # one `tezcat-revert` away regardless
    assert "renew the domain" not in (vault / "Harness/Capture.md").read_text()
    assert any("ingest into" in c["subject"] for c in Vault(vault).history(10))


def test_ingest_queues_when_the_drafter_flags_review(vault, wkw):
    """needs_review (a dropped impossible date) forces the checkbox even
    on the auto-trusted path — ingest returns the queued proposal."""

    async def extractor(text, today):
        return [{"text": "call the bank", "dropped_due": "2026-02-30"}]

    result = asyncio.run(service.ingest(
        str(vault), "call the bank on feb 30",
        capture_extractor=extractor,
        disabled=("inbox_extract", "spec_drift", "schema_fix", "grouping", "archive_done"),
        **wkw))
    (queued,) = result["queued"]
    assert queued["kind"] == "capture" and queued["needs_review"]
    assert "call the bank" not in (vault / "Tasks/Inbox.md").read_text()


def test_ingest_empty_text_is_a_no_op(vault, wkw):
    result = asyncio.run(service.ingest(str(vault), "   \n ", **wkw))
    assert result == {"summary": {}, "queued": [], "deferred": False}


def test_revert_undoes_a_snapshot_and_rejects_junk(vault, wkw):
    p = plant_proposal(vault, wkw)
    asyncio.run(service.decide(str(vault), p["id"], "approve", **wkw))
    from tezcat_vault.vault import Vault

    sha = next(c["sha"] for c in Vault(vault).history(10)
               if "Tasks/Inbox.md" in c["files"])
    subject = service.revert(str(vault), sha)
    assert subject  # the reverted commit's subject line
    assert "planted task" not in (vault / "Tasks/Inbox.md").read_text()
    with pytest.raises(ValueError):
        service.revert(str(vault), "not-a-sha")


def test_partial_approval_applies_only_the_chosen_steps(vault, wkw):
    """The prototype-parity amendment: a capture proposal approved with
    a steps subset applies exactly those steps; the trimmed queue file
    is the consent record."""
    p = proposals.new_proposal(
        kind="capture", note="Harness/Capture.md", vault_sha="",
        note_content="renew the domain\ncall the bank",
        title="Add 2 captured task(s)", summary="s",
        issues=["renew the domain", "call the bank"],
        apply=[
            {"tool": "add_task", "args": {"path": "Tasks/Inbox.md", "text": "renew the domain"}},
            {"tool": "add_task", "args": {"path": "Tasks/Inbox.md", "text": "call the bank"}},
        ])
    # partial staleness keys on capture text; plant the surface to match
    (vault / "Harness/Capture.md").write_text("renew the domain\ncall the bank\n")
    proposals.save(p, wkw["proposals_dir"])
    summary = asyncio.run(service.decide(str(vault), p["id"], "approve",
                                         steps=[0], **wkw))
    assert summary["approved"] == 1
    inbox = (vault / "Tasks/Inbox.md").read_text()
    assert "renew the domain" in inbox and "call the bank" not in inbox
    resolved = [x for x in proposals.load_all(wkw["proposals_dir"]) if x["id"] == p["id"]]
    assert len(resolved[0]["apply"]) == 1  # the trimmed file is the record


def test_partial_approval_with_no_steps_is_a_reject(vault, wkw):
    p = plant_proposal(vault, wkw)
    summary = asyncio.run(service.decide(str(vault), p["id"], "approve",
                                         steps=[], **wkw))
    assert summary["rejected"] == 1
    assert "planted task" not in (vault / "Tasks/Inbox.md").read_text()


def test_partial_approval_validates_step_range(vault, wkw):
    p = plant_proposal(vault, wkw)
    with pytest.raises(ValueError, match="out of range"):
        asyncio.run(service.decide(str(vault), p["id"], "approve", steps=[9], **wkw))


def test_complete_task_checks_off_through_the_gated_tool(vault, wkw):
    (vault / "Tasks/Inbox.md").write_text(
        "## Tasks\n\n- [ ] rotate the leaked service key\n")
    service.complete_task(str(vault), "Tasks/Inbox.md", "rotate the leaked service key")
    text = (vault / "Tasks/Inbox.md").read_text()
    assert "- [x] rotate the leaked service key" in text
    from tezcat_vault.vault import Vault

    assert Vault(vault).history(3)[0]["files"] == ["Tasks/Inbox.md"]  # snapshotted


def test_pass_emits_an_event_the_stream_can_tail(vault, wkw):
    offset, _ = events.read_new(str(vault), 0)
    asyncio.run(Watcher(str(vault), **wkw).run_once())
    offset, recs = events.read_new(str(vault), offset)
    assert any(r["event"] == "pass_complete" for r in recs)
    rec = [r for r in recs if r["event"] == "pass_complete"][-1]
    assert "pending" in rec and "summary" in rec
    # tailing from the new offset yields nothing until the next pass
    assert events.read_new(str(vault), offset) == (offset, [])


def test_event_emission_never_fails_the_pass(vault, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", boom)
    # emit swallows the OSError; nothing raises
    events.emit(str(vault), "pass_complete", {"summary": {}, "pending": 0})


# ---- the owned editor's note channel ----


def test_read_note_is_byte_exact(vault):
    """The editor's document IS the file. No parse, no normalization —
    round-tripping through read/write must not touch a byte."""
    rel = "Meetings/2026-07-17 Q3 Planning.md"
    on_disk = (vault / rel).read_text()
    got = service.read_note(str(vault), rel)
    assert got["text"] == on_disk
    assert got["hash"] == proposals.content_hash(on_disk)


def test_write_note_snapshots_and_is_revertible(vault):
    rel = "Home.md"
    before = service.read_note(str(vault), rel)
    edited = before["text"] + "\nA line I typed.\n"
    result = service.write_note(str(vault), rel, edited, before["hash"])
    assert result["written"] is True
    assert (vault / rel).read_text() == edited          # byte-exact save
    assert any("edit Home.md" in c["subject"] for c in Vault(vault).history(5))


def test_write_note_no_op_when_unchanged(vault):
    """Saving an untouched buffer must not manufacture a snapshot — the
    churn discipline the surfaces already follow."""
    rel = "Home.md"
    before = service.read_note(str(vault), rel)
    n = len(Vault(vault).history(20))
    result = service.write_note(str(vault), rel, before["text"], before["hash"])
    assert result["written"] is False
    assert len(Vault(vault).history(20)) == n


def test_write_note_refuses_to_clobber_a_moved_note(vault):
    """The engine auto-applies while you type. A save against a stale base
    must fail loud, not silently eat the change that landed."""
    rel = "Home.md"
    opened = service.read_note(str(vault), rel)
    (vault / rel).write_text(opened["text"] + "\nthe engine got here first\n")
    with pytest.raises(RuntimeError, match="changed on disk"):
        service.write_note(str(vault), rel, "my version\n", opened["hash"])
    assert "the engine got here first" in (vault / rel).read_text()


def test_note_channel_cannot_reach_excluded_folders(vault):
    """Private/ is invisible to the editor exactly as it is to the model."""
    with pytest.raises(VaultError):
        service.read_note(str(vault), "Private/Client Notes.md")


def test_a_decision_waits_for_the_pass_lock_instead_of_evaporating(vault, wkw):
    """The owned-UI regression: decide() used to take the pass lock
    NON-blocking, so clicking approve while a pass was drafting returned
    summary["locked"] and threw the verdict away — while the dashboard
    optimistically rendered it as decided. A checkbox has a next tick; a
    click does not, so it queues."""
    import fcntl
    p = plant_proposal(vault, wkw)
    lock_path = wkw["state_path"].with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)

    async def scenario():
        decision = asyncio.ensure_future(
            service.decide(str(vault), p["id"], "approve", **wkw))
        await asyncio.sleep(0.3)          # it must still be waiting, not done
        assert not decision.done()
        fcntl.flock(holder, fcntl.LOCK_UN)   # the "other pass" finishes
        holder.close()
        return await decision

    summary = asyncio.run(scenario())
    assert summary["approved"] == 1
    assert "planted task" in (vault / "Tasks/Inbox.md").read_text()


def test_a_decision_that_never_gets_the_lock_fails_loud(vault, wkw, monkeypatch):
    """And when the wait really does expire, it raises rather than
    reporting a pass that quietly did nothing."""
    import fcntl

    from tezcat import watcher as watcher_mod
    monkeypatch.setattr(watcher_mod, "DECISION_LOCK_WAIT_S", 0.2)
    monkeypatch.setattr(service, "DECISION_LOCK_WAIT_S", 0.2)
    p = plant_proposal(vault, wkw)
    lock_path = wkw["state_path"].with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        with pytest.raises(watcher_mod.LockBusy):
            asyncio.run(service.decide(str(vault), p["id"], "approve", **wkw))
    # the proposal is untouched and still decidable
    assert [x["id"] for x in proposals.pending(wkw["proposals_dir"])] == [p["id"]]


def test_a_lost_lock_restores_a_partially_approved_proposal(vault, wkw, monkeypatch):
    """Partial approval trims the queue file BEFORE the pass. If the pass
    never runs, that trim must not survive — the queue file is the consent
    record, and a silently redefined proposal is what the user would then
    retry on."""
    import fcntl

    from tezcat import watcher as watcher_mod
    monkeypatch.setattr(watcher_mod, "DECISION_LOCK_WAIT_S", 0.2)
    monkeypatch.setattr(service, "DECISION_LOCK_WAIT_S", 0.2)
    p = proposals.new_proposal(
        kind="capture", note="Home.md", vault_sha="",
        note_content=(vault / "Home.md").read_text(),
        title="two tasks", summary="s", issues=["a", "b"],
        apply=[{"tool": "add_task", "args": {"path": "Tasks/Inbox.md", "text": "one"}},
               {"tool": "add_task", "args": {"path": "Tasks/Inbox.md", "text": "two"}}])
    proposals.save(p, wkw["proposals_dir"])
    lock_path = wkw["state_path"].with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        with pytest.raises(watcher_mod.LockBusy):
            asyncio.run(service.decide(str(vault), p["id"], "approve",
                                       steps=[0], **wkw))
    (still,) = proposals.pending(wkw["proposals_dir"])
    assert len(still["apply"]) == 2, "the trim outlived the decision that failed"


def test_ingest_defers_rather_than_losing_the_dump(vault, wkw, monkeypatch):
    """Unlike a decision, a canvas dump is already durable on the capture
    surface — so losing the lock is deferral, not failure, and it says so."""
    import fcntl

    from tezcat import watcher as watcher_mod
    monkeypatch.setattr(watcher_mod, "DECISION_LOCK_WAIT_S", 0.2)
    monkeypatch.setattr(service, "DECISION_LOCK_WAIT_S", 0.2)
    lock_path = wkw["state_path"].with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        result = asyncio.run(service.ingest(str(vault), "call the bank", **wkw))
    assert result["deferred"] is True
    assert "call the bank" in (vault / "Harness/Capture.md").read_text()
