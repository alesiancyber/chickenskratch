"""The proposal pipeline end to end:
scan → draft → queue → decide → apply through the gated tools, with
staleness and the git memo. A scripted fake drafter stands in for the
worker LLM, same philosophy as FakeOpenAI.

Decisions arrive as `decisions=[(pid, verdict)]` — the one consent
channel. It is exactly what service.decide hands the engine for a
dashboard button or `chickenskratch decide`, so these tests exercise the
production path."""

import asyncio
import json

import pytest

from chickenskratch import proposals
from chickenskratch.watcher import Watcher
from chickenskratch_vault import vaultconfig as vc
from chickenskratch_vault.vault import Vault


async def fake_drafter(item):
    """Deterministic plan: stamp the note-level id the scan allocated."""
    return {
        "title": f"Normalize {item['note']}",
        "summary": "stamp the entity id",
        "apply": [
            {"tool": "update_frontmatter",
             "args": {"path": item["note"], "key": "id", "value": item["new_id"]}},
        ],
    }


@pytest.fixture()
def vault(gitvault):
    # this suite tests the checkbox path — revoke auto-apply trust
    (gitvault / "Harness").mkdir(exist_ok=True)
    (gitvault / "Harness/Spec.md").write_text('---\nauto_apply: []\n---\n\n# Vault Spec\n')
    return gitvault


@pytest.fixture()
def watcher(vault, make_watcher):
    return make_watcher(vault, drafter=fake_drafter, n_max=3)


def run(watcher):
    return asyncio.run(watcher.run_once())


def decide(watcher, pid, verdict="approve", **kw):
    """One decision through the engine — the button's path."""
    return asyncio.run(watcher.run_once(decisions=[(pid, verdict)], **kw))


def test_first_pass_scans_and_drafts_into_the_queue(vault, watcher):
    summary = run(watcher)
    assert summary["scanned"] and summary["drafted"] == 3  # capped at n_max

    rows = proposals.pending(watcher.proposals_dir)
    assert len(rows) == 3
    assert all(p["kind"] == "schema_fix" and p["status"] == "pending" for p in rows)
    assert not any(p["drafted_against"]["note"].startswith("Private/") for p in rows)

    # The queue in data/ is the whole record: a pass drafts proposals and
    # writes NOTHING into the vault — the retired markdown surfaces must
    # never come back as files.
    assert not (vault / "Harness/Review.md").exists()
    retired = {"Harness/Review.md", "Harness/Today.md", "Harness/Changes.md"}
    assert not any(f in retired for c in Vault(vault).history() for f in c["files"])


def test_an_unchanged_vault_keeps_draining_its_backlog(vault, watcher):
    """Every pass scans — there is no vault memo — so n_max is a per-pass
    budget, not a cap on the whole backlog.

    The memo made this wrong in a way nothing caught: proposals land in
    data/, which does not move the vault fingerprint, so after the first
    pass drafted its n_max the tree read "unchanged" and every later pass
    skipped the scan. The remaining candidates were unreachable until
    some unrelated edit happened to move the fingerprint."""
    first = run(watcher)
    assert first["drafted"] == 3            # n_max
    second = run(watcher)
    assert second["scanned"] and second["drafted"] == 3   # the next three
    assert len(proposals.pending(watcher.proposals_dir)) == 6


def test_approve_applies_and_records(vault, watcher, monkeypatch):
    recorded = []
    monkeypatch.setattr("chickenskratch.watcher.record_dashboard", lambda p, d, **k: recorded.append((p["id"], d)))
    run(watcher)
    target = proposals.pending(watcher.proposals_dir)[0]
    note = target["drafted_against"]["note"]
    stamped_id = target["apply"][0]["args"]["value"]

    summary = decide(watcher, target["id"])

    assert summary["approved"] == 1
    assert f"id: {stamped_id}" in (vault / note).read_text()   # the fix landed
    stored = json.loads((watcher.proposals_dir / f"{target['id']}.json").read_text())
    assert stored["status"] == "approved" and stored["resolved"]["decision"] == "approved"
    assert (target["id"], "approved") in recorded
    assert target["id"] not in {p["id"] for p in proposals.pending(watcher.proposals_dir)}
    # applying is a normal gated mutation: its commit is in history
    assert any("frontmatter" in c["subject"] for c in Vault(vault).history())


def test_reject_records_without_touching_vault(vault, watcher, monkeypatch):
    recorded = []
    monkeypatch.setattr("chickenskratch.watcher.record_dashboard", lambda p, d, **k: recorded.append(d))
    run(watcher)
    target = proposals.pending(watcher.proposals_dir)[0]
    before = (vault / target["drafted_against"]["note"]).read_text()

    summary = decide(watcher, target["id"], "reject")

    assert summary["rejected"] == 1 and recorded == ["rejected"]
    assert (vault / target["drafted_against"]["note"]).read_text() == before


def test_hand_edit_between_draft_and_approve_goes_stale(vault, watcher):
    run(watcher)
    target = proposals.pending(watcher.proposals_dir)[0]

    note = vault / target["drafted_against"]["note"]
    before_apply = note.read_text() + "\nA hand edit after drafting.\n"
    note.write_text(before_apply)

    summary = decide(watcher, target["id"])

    assert summary["approved"] == 0 and summary["stale"] >= 1
    assert note.read_text() == before_apply                     # never blind-applied
    stored = json.loads((watcher.proposals_dir / f"{target['id']}.json").read_text())
    assert stored["status"] == "stale"


def test_decisions_never_act_twice(vault, watcher):
    run(watcher)
    target = proposals.pending(watcher.proposals_dir)[0]
    first = decide(watcher, target["id"])
    assert first["approved"] == 1

    # a double-click, a retried request, a stale dashboard: same verdict again
    second = decide(watcher, target["id"])
    assert second["approved"] == 0  # resolved proposals are not pending — no re-apply

    # and the same pid twice WITHIN one call resolves exactly once
    run(watcher)
    other = proposals.pending(watcher.proposals_dir)[0]
    dup = asyncio.run(watcher.run_once(
        decisions=[(other["id"], "approve"), (other["id"], "approve")]))
    assert dup["approved"] == 1


def test_decisions_only_acts_without_scanning(vault, watcher, make_watcher):
    """The UI button's lane: apply the verdict now, never draft — a click
    must not wait on a worker."""
    run(watcher)
    target = proposals.pending(watcher.proposals_dir)[0]

    async def exploding(item):
        raise AssertionError("the decision lane must never draft")

    fast = make_watcher(vault, drafter=exploding, n_max=3)
    summary = decide(fast, target["id"], decisions_only=True)
    assert summary["approved"] == 1 and summary["drafted"] == 0 and not summary["scanned"]
    # the memo was left untouched, so the next full pass still scans
    assert run(watcher)["scanned"]


def test_an_auto_apply_lands_and_the_next_pass_reads_what_it_wrote(vault, make_watcher):
    """Auto-apply is the one thing that still writes to the vault AFTER
    this pass's scan, so it is what proves the next pass reads the tree
    as the apply left it rather than as the scan found it."""
    from tests.conftest import record_worker_returning

    # this suite's vault revokes auto-apply trust; this test needs it back
    (vault / "Harness/Spec.md").write_text(
        "---\npolicies: {capture: verified}\n---\n\n# Vault Spec\n")
    worker = record_worker_returning(
        [{"text": "buy a car", "due": None, "priority": None, "owed_by": None}])
    w = make_watcher(vault, record_worker=worker,
                     disabled=("schema_fix", "grouping", "spec_drift"),
                     n_max=20)
    (vault / "Harness/Capture.md").write_text("# Capture\n\nbuy a car\n")
    assert run(w)["auto"] == 1        # applied after the scan had already run
    import datetime as _dt
    filed = (vault / "Captures" / f"{_dt.date.today().isoformat()}.md").read_text()
    assert "buy a car" in filed
    # the next pass sees the cleared surface and does not re-extract
    assert run(w)["auto"] == 0


def test_invalid_drafter_output_never_enters_queue(vault, tmp_path, monkeypatch):
    monkeypatch.setattr("chickenskratch.watcher.record_dashboard", lambda *a, **k: None)

    async def bad_drafter(item):
        return {"title": "x", "summary": "y",
                "apply": [{"tool": "delete_everything", "args": {}}]}

    w = Watcher(str(vault), proposals_dir=tmp_path / "p2",
                state_path=tmp_path / "s2.json", drafter=bad_drafter, n_max=2)
    summary = run(w)
    assert summary["drafted"] == 0
    assert proposals.pending(w.proposals_dir) == []


def test_a_seenless_lane_parks_on_its_budget_not_forever(vault, tmp_path, monkeypatch):
    """schema_fix re-finds its items every pass and stamps no seen-hash,
    so parking must ride the budget slot itself. Before the slot check
    in _draft, a plan that always failed its schema burned one worker
    call per pass forever — while the log claimed it had parked."""
    monkeypatch.setattr("chickenskratch.watcher.record_dashboard", lambda *a, **k: None)
    from chickenskratch import watcher as watcher_mod
    calls = []

    async def bad_drafter(item):
        calls.append(item["note"])
        return {"title": "", "summary": "s",   # empty title fails the proposal schema
                "apply": [{"tool": "update_frontmatter",
                           "args": {"path": item["note"], "key": "id",
                                    "value": item["new_id"]}}]}

    def engine():
        return Watcher(str(vault), proposals_dir=tmp_path / "pq",
                       state_path=tmp_path / "sq.json", drafter=bad_drafter,
                       n_max=20, disabled=("capture", "grouping",
                                           "spec_drift", "overlap", "plan_today"))

    for i in range(1, watcher_mod.DRAFT_PLAN_ATTEMPTS + 1):
        errs = run(engine())["worker_errors"]
        assert errs and all(
            e["retrying"] is (i < watcher_mod.DRAFT_PLAN_ATTEMPTS) for e in errs)
    burned = len(calls)
    # parked: a further pass never reaches the drafter, and reports no
    # retry it won't make — the budget entry itself holds each input
    assert run(engine())["worker_errors"] == []
    assert len(calls) == burned


def test_proposal_save_validates():
    with pytest.raises(ValueError, match="invalid proposal"):
        proposals.save({"schema_version": 1, "id": "p-nothexdigits"})


def test_default_state_and_queue_are_per_vault(tmp_path):
    """Two vaults on one checkout must not share a scan memo, a pass
    lock, or a proposal queue (a second vault's pass was auto-staling
    the first vault's pending proposals via the shared queue)."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    wa, wb = Watcher(str(a)), Watcher(str(b))
    assert wa.proposals_dir != wb.proposals_dir
    assert wa.state_path != wb.state_path


def test_typed_json_values_apply_cleanly(vault, watcher):
    """The 2026-07-23 wedge, first half: a drafter emitting a typed JSON
    value (null) in update_frontmatter must apply as that value, not
    crash yaml.safe_load."""
    from chickenskratch import applier
    applier.use_vault(str(vault))
    from chickenskratch_vault import server

    p = proposals.new_proposal(
        kind="schema_fix", note="Home.md", vault_sha="",
        note_content=(vault / "Home.md").read_text(),
        title="typed value", summary="s", issues=[],
        apply=[{"tool": "update_frontmatter",
                "args": {"path": "Home.md", "key": "due", "value": None}}])
    applier.apply(p, str(vault))
    assert server.read_note("Home.md")["frontmatter"]["due"] is None
    # simulate mirrors apply exactly
    after = applier.simulate((vault / "Home.md").read_text(), p)
    assert after is not None


def test_poison_apply_fails_the_proposal_never_the_pass(vault, watcher):
    """The 2026-07-23 wedge, second half: an approved proposal whose
    apply raises must resolve as `failed` — left pending, the checked
    box would re-crash every subsequent pass forever."""
    run(watcher)
    good = proposals.pending(watcher.proposals_dir)[0]
    poison = proposals.new_proposal(
        kind="schema_fix", note="Home.md", vault_sha="",
        note_content=(vault / "Home.md").read_text(),
        title="poison", summary="s", issues=[],
        apply=[{"tool": "replace_section",
                "args": {"path": "Nope/Missing.md", "heading": "X", "content": "y"}}])
    proposals.save(poison, watcher.proposals_dir)

    summary = asyncio.run(watcher.run_once(
        decisions=[(poison["id"], "approve"), (good["id"], "approve")]))
    assert summary["failed"] == 1
    assert summary["approved"] == 1  # the good approval still landed
    statuses = {q["id"]: q["status"] for q in proposals.load_all(watcher.proposals_dir)}
    assert statuses[poison["id"]] == "failed"
    assert statuses[good["id"]] == "approved"
    # resolved, not left pending: a poison plan must never be retryable
    assert poison["id"] not in {q["id"] for q in proposals.pending(watcher.proposals_dir)}

    # and the next pass is healthy — no retry loop
    assert run(watcher)["failed"] == 0


def test_seen_state_is_one_keyed_map(watcher, vault):
    """The extractors' seen-hashes persist as one name-keyed map, so a new
    extractor needs no engine-side field. Empties never persist."""
    assert watcher._seen_from_state({"seen": {"capture": "abc"}}) == {"capture": "abc"}
    assert watcher._seen_from_state(
        {"seen": {"capture": "new", "overlap": ""}}) == {"capture": "new"}

    watcher.seen = {"capture": "hash-1"}
    watcher._save_state()
    assert json.loads(watcher.state_path.read_text())["seen"] == {"capture": "hash-1"}


def test_stale_rearms_its_processor_generically(watcher):
    """A stale proposal drops its kind's seen-hash — the re-draft must be
    guaranteed (the 2026-07-24 silent-drop bug). Kinds that keep no
    seen-hash simply have nothing to drop."""
    watcher.seen = {"capture": "hash-1", "overlap": "hash-2"}
    watcher._notify_stale({"kind": "capture"})
    assert watcher.seen == {"overlap": "hash-2"}
    watcher._notify_stale({"kind": "schema_fix"})  # no seen-hash: no-op
    assert watcher.seen == {"overlap": "hash-2"}


def _spec_with(**kw):
    """A one-off processor whose scan/draft always produce one proposal."""
    from chickenskratch import proposals as pr
    from chickenskratch.processors import ProcessorSpec

    def scan(w):
        # the REAL note text, or the proposal is stale before it is decided
        return [{"kind": "boom", "note": "Home.md",
                 "content": w.vault.read("Home.md"), "vault_sha": w._pass_sha}]

    async def draft(w, item):
        return pr.new_proposal(
            kind="boom", note="Home.md", vault_sha="", note_content=item["content"],
            title="t", summary="s", issues=[],
            apply=[{"tool": "add_task",
                    "args": {"path": "Tasks/Inbox.md", "text": "hooked"}}])

    return ProcessorSpec("boom", scan, draft, **kw)


def test_a_raising_verifier_queues_instead_of_killing_the_pass(vault, make_watcher):
    """Verification degrades to review — that is the whole contract of the
    auto-apply gate. A verifier that RAISES didn't vouch either, so it
    must mean the same thing as one that returns failures."""
    def boom(w, item, p):
        raise RuntimeError("verifier blew up")

    (vault / "Harness").mkdir(exist_ok=True)
    (vault / "Harness" / "Spec.md").write_text("---\nauto_apply: [boom]\n---\n")
    vc._merged_cache.clear()
    w = make_watcher(vault, registry=(_spec_with(verify=boom),))
    summary = asyncio.run(w.run_once())
    vc._merged_cache.clear()
    assert summary["drafted"] == 1 and summary["auto"] == 0   # kept its checkbox
    assert len(proposals.pending(w.proposals_dir)) == 1


def test_a_raising_post_apply_hook_does_not_lose_the_apply(vault, make_watcher):
    """By the time on_applied runs the change is written, committed, and
    resolved. Letting the hook raise would abandon a correct apply with an
    exception and tell the user it failed."""
    def boom(w, p):
        raise RuntimeError("post-hook blew up")

    w = make_watcher(vault, registry=(_spec_with(on_applied=boom),))
    asyncio.run(w.run_once())
    pid = proposals.pending(w.proposals_dir)[0]["id"]
    summary = asyncio.run(w.run_once(decisions_only=True,
                                     decisions=[(pid, "approve")], lock_wait=5))
    assert summary["approved"] == 1
    assert "hooked" in (vault / "Tasks/Inbox.md").read_text()
    assert proposals.pending(w.proposals_dir) == []


# --------------- work a pass did not reach is not lost ---------------
#
# These used to assert summary["scanned"] against the vault memo. The memo
# is gone — every pass scans — so the claim worth making is the OUTCOME it
# was protecting: whatever a pass didn't get to, the next one does.


def test_a_note_written_mid_pass_is_drafted_by_the_next_pass(vault, make_watcher):
    """The user typing during a slow worker call. The note lands after
    this pass's scan has already walked the tree, so nothing here can see
    it — but it must not need an unrelated edit later to become visible."""
    # a typed note the entity schema will flag — untyped files are nobody's
    # candidate, so they could never prove anything about being picked up
    mid = vault / "Tasks" / "typed-mid-pass.md"

    async def drafter_racing_a_user_edit(item):
        mid.write_text("---\ntype: research\n---\n\n# Typed mid-pass\n\ncall the vendor\n")
        return await fake_drafter(item)

    w = make_watcher(vault,                      drafter=drafter_racing_a_user_edit, n_max=1)
    run(w)
    assert mid.exists()
    def drafted(wt):
        return {p["drafted_against"]["note"] for p in proposals.pending(wt.proposals_dir)}

    assert "Tasks/typed-mid-pass.md" not in drafted(w)   # this pass never saw it
    w2 = make_watcher(vault, drafter=fake_drafter, n_max=50)
    run(w2)
    assert "Tasks/typed-mid-pass.md" in drafted(w2)


def test_a_cancelled_pass_leaves_its_work_for_the_next_one(vault, make_watcher):
    """A stop request ends the pass before it drafts. The next pass has to
    pick that work up — a stop is a pause, never a silent drop."""
    w = make_watcher(vault, drafter=fake_drafter,
                     n_max=3, should_cancel=lambda: True)
    summary = run(w)
    assert summary["cancelled"] and summary["drafted"] == 0
    w2 = make_watcher(vault, drafter=fake_drafter, n_max=3)
    assert run(w2)["drafted"] == 3


# --------------- one content walk per pass ---------------


def test_the_pass_walks_the_vault_exactly_once(vault, watcher, monkeypatch):
    """Commands, schema validation, and grouping all consume the shared
    pass_notes cache: a scanned pass costs ONE iter_knowledge_notes walk.
    A second walk appearing here means a consumer regressed to reading
    the vault itself."""
    walks = []
    original = Vault.iter_knowledge_notes

    def counting(self, *args, **kwargs):
        walks.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Vault, "iter_knowledge_notes", counting)
    assert run(watcher)["scanned"]
    assert len(walks) == 1


# --------------- hooks outlive disabling ---------------


def test_capture_approved_after_disabling_still_resets_the_surface(vault, make_watcher):
    """Disabling a processor stops new scans, not the bookkeeping of
    already-drafted work: a pending capture proposal approved after
    `disabled_processors: [capture]` still runs on_applied, so the
    surface resets instead of re-offering the same dump forever."""
    from chickenskratch.processors import CAPTURE_SKELETON
    from tests.conftest import record_worker_returning

    # the summary keeps it on the queue path (bare lists auto-file now)
    worker = record_worker_returning(
        [{"text": "renew the domain", "due": None, "priority": None, "owed_by": None}],
        summary="The domain needs renewing.")
    (vault / "Harness" / "Capture.md").write_text("renew the domain\n")
    drafting = make_watcher(vault, record_worker=worker,
                            drafter=fake_drafter)
    run(drafting)
    pending = proposals.pending(drafting.proposals_dir)
    pid = next(p["id"] for p in pending if p["kind"] == "capture")

    deciding = make_watcher(vault, disabled=("capture",),
                            drafter=fake_drafter)
    summary = decide(deciding, pid, lock_wait=5)
    assert summary["approved"] == 1
    assert (vault / "Harness" / "Capture.md").read_text() == CAPTURE_SKELETON
