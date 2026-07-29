"""Phase 7: the engine is generic. Processors
are registry entries, priority is registry order, rollback is a name in
`disabled`, and concurrent passes are serialized by the pass lock."""

import asyncio
import fcntl

import pytest

from tezcat import applier, processors, proposals
from tezcat.processors import ProcessorSpec
from tezcat_vault.vault import Vault


@pytest.fixture()
def vault(gitvault):
    # unified diffs + no auto: this suite asserts the queue path literally
    (gitvault / "Harness").mkdir(exist_ok=True)
    (gitvault / "Harness/Spec.md").write_text('---\nauto_apply: []\n---\n\n# Vault Spec\n')
    return gitvault


def run(w):
    return asyncio.run(w.run_once())


def test_default_registry_order_is_priority_order():
    # capture first: active user input must never be starved out of n_max;
    # plan_today last: a suggestion must never starve real maintenance
    # (//commands are a pass phase before drafting, not a processor)
    assert [p.name for p in processors.REGISTRY] == [
        "capture", "spec_drift", "schema_fix", "overlap", "plan_today", "grouping"]


def test_disable_is_per_processor_and_unknown_names_fail_loud(vault, make_watcher):
    w = make_watcher(
        vault, disabled=("schema_fix", "grouping", "spec_drift",
                         "overlap", "plan_today"))
    assert [p.name for p in w.processors] == ["capture"]
    with pytest.raises(ValueError, match="groupnig"):
        processors.registry(disabled=("groupnig",))


def test_fourth_processor_runs_end_to_end_without_engine_changes(vault, make_watcher):
    """A processor the engine has never heard of: scanned, drafted,
    queued, approved, applied, hook fired."""
    applied_hook = []

    def scan(w):
        sha, _ = applier.vault_state(w.vault_path)
        v = Vault(w.vault_path)
        note = "Daily/2026-07-16.md"
        return [{"kind": "stamp_footer", "note": note, "vault_sha": sha,
                 "content": v.read(note)}]

    async def draft(w, item):
        return proposals.new_proposal(
            kind="stamp_footer",
            note=item["note"],
            vault_sha=item["vault_sha"],
            note_content=item["content"],
            title=f"Stamp footer on {item['note']}",
            summary="adds a footer line",
            issues=["missing footer"],
            apply=[{"tool": "append_to_note",
                    "args": {"path": item["note"], "content": "reviewed ✓"}}],
        )

    spec = ProcessorSpec(
        "stamp_footer", scan, draft,
        on_applied=lambda w, p: applied_hook.append(p["kind"]),
    )
    w = make_watcher(vault, registry=(spec,))
    run(w)
    (p,) = proposals.pending(w.proposals_dir)
    assert p["kind"] == "stamp_footer"
    assert p["title"] == "Stamp footer on Daily/2026-07-16.md"

    summary = asyncio.run(w.run_once(decisions=[(p["id"], "approve")]))
    assert summary["approved"] == 1
    assert "reviewed ✓" in (vault / "Daily/2026-07-16.md").read_text()
    assert applied_hook == ["stamp_footer"]


def test_concurrent_pass_is_skipped_by_the_lock(vault, make_watcher):
    w = make_watcher(vault, registry=())
    lock_path = w.state_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        summary = run(w)
        assert summary["locked"] is True
        assert summary["scanned"] is False and summary["drafted"] == 0
    finally:
        holder.close()
    summary = run(w)
    assert summary["locked"] is False  # lock released -> pass runs


def test_one_mis_encoded_note_does_not_disable_schema_fixing(vault, make_watcher):
    """validate_schema walks with read_lossy, so it reports notes the
    strict read then rejects — and the engine catches scan failures per
    PROCESSOR, so one latin-1 note used to silently kill schema fixing
    entirely, every pass. The bad note loses its fix; the others keep
    theirs."""
    from tezcat import applier
    (vault / "Meetings").mkdir(exist_ok=True)
    (vault / "Meetings" / "2026-07-01 Bad.md").write_bytes(
        "---\ntype: meeting\n---\n# Caf\xe9 sync\n".encode("latin-1"))
    (vault / "Meetings" / "2026-07-02 Good.md").write_text(
        "---\ntype: meeting\n---\n# Sync\n")
    applier.use_vault(str(vault))
    w = make_watcher(vault)
    notes = [i["note"] for i in processors.scan_schema(w)]
    assert "Meetings/2026-07-02 Good.md" in notes
    assert "Meetings/2026-07-01 Bad.md" not in notes


# --------------- plan_today: the judgment lane ---------------


PLAN_INBOX = (
    "## Tasks\n\n"
    "- [ ] chase the SOW redlines ➕ 2026-07-10 ^t-aaaaaaaaaaaa\n"
    "- [ ] read the auth RFC ^t-bbbbbbbbbbbb\n"
    "- [ ] has a deadline 📅 2099-01-01 ^t-cccccccccccc\n"      # dated: not pool
    "- [ ] already planned ⏳ 2099-01-01 ^t-dddddddddddd\n"     # planned: not pool
    "- [ ] unstamped so not yet eligible\n"                     # no id: not pool
    "  - [ ] a subtask ^t-eeeeeeeeeeee\n"                       # nested: not pool
)


def _plan_drafter(picks):
    calls = []

    async def drafter(item):
        calls.append(item)
        return {"picks": picks}

    drafter.calls = calls
    return drafter


def test_plan_scan_pools_only_the_undated_unplanned_stamped(vault, make_watcher):
    import datetime as dt

    (vault / "Tasks/Inbox.md").write_text(PLAN_INBOX)
    drafter = _plan_drafter([])
    w = make_watcher(vault, registry=(processors.PLAN_TODAY,), plan_drafter=drafter)
    run(w)
    (item,) = drafter.calls
    # (Task, rel) tuples: the pool spans notes now, oldest ➕ first
    # (t-aaaa has a birth date, t-bbbb sorts behind it without one)
    assert [(t.block_id, rel) for t, rel in item["tasks"]] == [
        ("t-aaaaaaaaaaaa", "Tasks/Inbox.md"), ("t-bbbbbbbbbbbb", "Tasks/Inbox.md")]

    # committed = open tasks due/planned today or earlier, vault-wide.
    # Relative check (the fixture vault has its own overdue tasks): one
    # more ⏳-today task bumps it by exactly one; the 2099 rows never do.
    before = item["committed"]
    today = dt.date.today().isoformat()
    (vault / "Tasks/Inbox.md").write_text(
        PLAN_INBOX
        + f"- [ ] started already ⏳ {today} ^t-f0f0f0f0f0f0\n"
        + "- [ ] fresh pool entry ^t-f1f1f1f1f1f1\n")  # re-arms the seen key
    run(w)
    assert drafter.calls[-1]["committed"] == before + 1


def test_overlap_scan_survives_a_missing_inbox(vault, make_watcher):
    """The re-anchor: overlap used to anchor its work item on the
    capture inbox and return [] when that note couldn't be read. With
    the inbox retired, that would have silently killed dedupe for good
    — a missing note reading as an empty vault, the exact class of lie
    the dead-worker and vault-memo fixes removed."""
    (vault / "Tasks/Inbox.md").unlink()
    (vault / "Tasks/A.md").write_text("- [ ] renew the certs ^t-aaaaaaaaaaaa\n")
    (vault / "Tasks/B.md").write_text("- [ ] renew the certs ^t-bbbbbbbbbbbb\n")
    w = make_watcher(vault)
    (item,) = processors.scan_overlap(w)
    assert item["note"] == "Tasks/A.md"      # anchored on the first pair's note
    ((a, _ra), (b, _rb)) = item["exact"][0]
    assert {a.block_id, b.block_id} == {"t-aaaaaaaaaaaa", "t-bbbbbbbbbbbb"}


def test_plan_pool_spans_records_and_skips_owed(vault, make_watcher):
    """plan_today's pool is the walk, not the inbox: a record's undated
    action items are plannable, and an owed 👤 line never is — planning
    somebody else's debt as your day is the double-count the partition
    exists to stop."""
    (vault / "Tasks/Inbox.md").unlink()
    (vault / "Meetings" / "2026-07-01 Kickoff.md").write_text(
        "# Kickoff\n\n## Action items\n\n"
        "- [ ] write the runbook ➕ 2026-07-01 ^t-aaaaaaaaaaaa\n"
        "- [ ] pipeline costs 👤 Nadia ➕ 2026-07-01 ^t-bbbbbbbbbbbb\n")
    w = make_watcher(vault)
    (item,) = processors.scan_plan(w)
    ids = {t.block_id for t, _rel in item["tasks"]}
    assert "t-aaaaaaaaaaaa" in ids
    assert "t-bbbbbbbbbbbb" not in ids
    assert item["note"] == "Meetings/2026-07-01 Kickoff.md"  # oldest task's home


def test_plan_empty_picks_is_an_answer_not_a_retry(vault, make_watcher):
    """The no-determination escape valve: 'nothing worth surfacing' must
    not re-ask the worker every pass — only tomorrow, or when the pool
    changes."""
    (vault / "Tasks/Inbox.md").write_text(PLAN_INBOX)
    drafter = _plan_drafter([])
    w = make_watcher(vault, registry=(processors.PLAN_TODAY,), plan_drafter=drafter)
    assert run(w)["drafted"] == 0
    (vault / "Tasks/Other.md").write_text("touch the tree\n")  # move the memo
    run(w)
    assert len(drafter.calls) == 1  # same pool, same day: never re-asked
    assert proposals.pending(w.proposals_dir) == []


def test_plan_broken_picks_never_enter_the_queue(vault, make_watcher):
    (vault / "Tasks/Inbox.md").write_text(PLAN_INBOX)
    drafter = _plan_drafter([{"id": "t-ffffffffffff", "reason": "invented"}])
    w = make_watcher(vault, registry=(processors.PLAN_TODAY,), plan_drafter=drafter)
    assert run(w)["drafted"] == 0
    assert proposals.pending(w.proposals_dir) == []


def test_plan_approval_stamps_the_plan_never_a_deadline(vault, make_watcher):
    """End to end: the pick queues (judgment never auto-applies — no
    verifier exists), approving stamps ⏳ today on exactly the picked
    task, and no 📅 appears anywhere."""
    import datetime as dt

    (vault / "Tasks/Inbox.md").write_text(PLAN_INBOX)
    drafter = _plan_drafter([{"id": "t-aaaaaaaaaaaa", "reason": "oldest, blocks legal"}])
    w = make_watcher(vault, registry=(processors.PLAN_TODAY,), plan_drafter=drafter)
    assert run(w)["drafted"] == 1 and run(w)["auto"] == 0

    (p,) = proposals.pending(w.proposals_dir)
    assert p["kind"] == "plan_today"
    assert p["apply"][0]["tool"] == "set_task_scheduled"
    assert "chase the SOW redlines — oldest, blocks legal" in p["issues"][0]

    summary = asyncio.run(w.run_once(decisions=[(p["id"], "approve")], lock_wait=5))
    assert summary["approved"] == 1
    text = (vault / "Tasks/Inbox.md").read_text()
    today = dt.date.today().isoformat()
    assert f"- [ ] chase the SOW redlines ⏳ {today} ➕ 2026-07-10 ^t-aaaaaaaaaaaa" in text
    assert "read the auth RFC ^t-bbbbbbbbbbbb" in text          # untouched
    assert text.count("📅") == 1                                 # only the planted one
