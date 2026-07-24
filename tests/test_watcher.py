"""The review-dashboard pipeline end to end:
scan → draft → render Review.md → checkbox approve/reject → apply through
the gated tools, with staleness and the git memo. A scripted fake drafter
stands in for the worker LLM, same philosophy as FakeOpenAI."""

import asyncio
import json

import pytest

from harness import proposals
from harness.watcher import Watcher
from obsidian_mcp.vault import Vault


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
    (gitvault / "Harness/Spec.md").write_text('---\nauto_apply: []\ndiff_style: unified\n---\n\n# Vault Spec\n')
    return gitvault


@pytest.fixture()
def watcher(vault, make_watcher):
    return make_watcher(vault, drafter=fake_drafter, n_max=3)


def run(watcher):
    return asyncio.run(watcher.run_once())


def test_first_pass_scans_drafts_and_renders(vault, watcher):
    summary = run(watcher)
    assert summary["scanned"] and summary["drafted"] == 3  # capped at n_max

    rows = proposals.pending(watcher.proposals_dir)
    assert len(rows) == 3
    assert all(p["kind"] == "schema_fix" and p["status"] == "pending" for p in rows)
    assert not any(p["drafted_against"]["note"].startswith("Private/") for p in rows)

    review = (vault / "Harness/Review.md").read_text()
    for p in rows:
        assert p["title"] in review
        assert f"- [ ] approve {p['id']}" in review
        assert f"- [ ] reject {p['id']}" in review
    assert "```diff" in review and "+id:" in review  # simulated frontmatter diff

    # the render is itself a snapshotted commit
    assert any("Harness/Review.md" in c["files"] for c in Vault(vault).history())


def test_unchanged_vault_second_pass_is_free(vault, watcher):
    run(watcher)
    before = (vault / "Harness/Review.md").read_text()
    second = run(watcher)
    assert not second["scanned"] and second["drafted"] == 0
    assert (vault / "Harness/Review.md").read_text() == before


def test_checkbox_approve_applies_and_records(vault, watcher, monkeypatch):
    recorded = []
    monkeypatch.setattr("harness.watcher.record_dashboard", lambda p, d, **k: recorded.append((p["id"], d)))
    run(watcher)
    target = proposals.pending(watcher.proposals_dir)[0]
    note = target["drafted_against"]["note"]
    stamped_id = target["apply"][0]["args"]["value"]

    review = vault / "Harness/Review.md"
    review.write_text(review.read_text().replace(
        f"- [ ] approve {target['id']}", f"- [x] approve {target['id']}"))
    summary = run(watcher)

    assert summary["approved"] == 1
    assert f"id: {stamped_id}" in (vault / note).read_text()   # the fix landed
    stored = json.loads((watcher.proposals_dir / f"{target['id']}.json").read_text())
    assert stored["status"] == "approved" and stored["resolved"]["decision"] == "approved"
    assert (target["id"], "approved") in recorded
    assert f"approve {target['id']}" not in review.read_text()  # re-rendered without it
    # applying is a normal gated mutation: its commit is in history
    assert any("frontmatter" in c["message"] for c in Vault(vault).history())


def test_checkbox_reject_records_without_touching_vault(vault, watcher, monkeypatch):
    recorded = []
    monkeypatch.setattr("harness.watcher.record_dashboard", lambda p, d, **k: recorded.append(d))
    run(watcher)
    target = proposals.pending(watcher.proposals_dir)[0]
    before = (vault / target["drafted_against"]["note"]).read_text()

    review = vault / "Harness/Review.md"
    review.write_text(review.read_text().replace(
        f"- [ ] reject {target['id']}", f"- [x] reject {target['id']}"))
    summary = run(watcher)

    assert summary["rejected"] == 1 and recorded == ["rejected"]
    assert (vault / target["drafted_against"]["note"]).read_text() == before


def test_hand_edit_between_draft_and_approve_goes_stale(vault, watcher):
    run(watcher)
    target = proposals.pending(watcher.proposals_dir)[0]

    note = vault / target["drafted_against"]["note"]
    before_apply = note.read_text() + "\nA hand edit after drafting.\n"
    note.write_text(before_apply)

    review = vault / "Harness/Review.md"
    review.write_text(review.read_text().replace(
        f"- [ ] approve {target['id']}", f"- [x] approve {target['id']}"))
    summary = run(watcher)

    assert summary["approved"] == 0 and summary["stale"] >= 1
    assert note.read_text() == before_apply                     # never blind-applied
    stored = json.loads((watcher.proposals_dir / f"{target['id']}.json").read_text())
    assert stored["status"] == "stale"


def test_decisions_never_act_twice(vault, watcher):
    run(watcher)
    target = proposals.pending(watcher.proposals_dir)[0]
    review = vault / "Harness/Review.md"
    review.write_text(review.read_text().replace(
        f"- [ ] approve {target['id']}", f"- [x] approve {target['id']}"))
    first = run(watcher)
    assert first["approved"] == 1

    # simulate a stray stale rendering with the same checked box
    review.write_text(f"- [x] approve {target['id']}\n")
    second = run(watcher)
    assert second["approved"] == 0  # resolved proposals are not pending — no re-apply


def test_decisions_only_acts_and_renders_without_scanning(vault, watcher, make_watcher):
    run(watcher)
    target = proposals.pending(watcher.proposals_dir)[0]
    review = vault / "Harness/Review.md"
    review.write_text(review.read_text().replace(
        f"- [ ] approve {target['id']}", f"- [x] approve {target['id']}"))

    async def exploding(item):
        raise AssertionError("the fast lane must never draft")

    fast = make_watcher(vault, drafter=exploding, n_max=3)
    summary = asyncio.run(fast.run_once(decisions_only=True))
    assert summary["approved"] == 1 and summary["drafted"] == 0 and not summary["scanned"]
    assert f"approve {target['id']}" not in review.read_text()  # feedback rendered
    # the memo was left untouched, so the next full pass still scans
    assert run(watcher)["scanned"]


def test_render_never_clobbers_unacted_checked_boxes(vault, watcher):
    run(watcher)
    target = proposals.pending(watcher.proposals_dir)[0]
    review = vault / "Harness/Review.md"
    checked = review.read_text().replace(
        f"- [ ] approve {target['id']}", f"- [x] approve {target['id']}")
    review.write_text(checked)
    assert watcher._render() is False     # unacted decision on the surface: hands off
    assert review.read_text() == checked  # the user's check survived
    assert run(watcher)["approved"] == 1  # and the next pass honors it


def test_mid_pass_decision_is_acted_before_the_pass_ends(vault, watcher, make_watcher):
    """The live deadlock of 2026-07-23: a box checked while a slow pass
    drafts must be honored by THAT pass — its trigger was consumed by the
    pass lock and re-baselining hides it from the loops."""
    run(watcher)  # leaves pending proposals
    target = proposals.pending(watcher.proposals_dir)[0]
    review = vault / "Harness/Review.md"

    async def checking_drafter(item):
        # the user's decision arrives mid-draft
        review.write_text(review.read_text().replace(
            f"- [ ] approve {target['id']}", f"- [x] approve {target['id']}"))
        return await fake_drafter(item)

    (vault / "Home.md").write_text((vault / "Home.md").read_text() + "\nnudge\n")
    summary = run(make_watcher(vault, drafter=checking_drafter, n_max=3))
    assert summary["approved"] == 1  # the late decision was swept in-pass
    assert f"approve {target['id']}" not in review.read_text()  # and re-rendered


def test_post_scan_applies_are_rescanned_next_pass(vault, watcher, make_watcher):
    """An apply that lands after this pass's scan (a decision swept
    mid-draft, an auto-apply) must not be folded into the git memo — the
    next full pass has to rescan the notes it changed."""
    run(watcher)  # leaves pending proposals
    target = proposals.pending(watcher.proposals_dir)[0]
    review = vault / "Harness/Review.md"

    async def checking_drafter(item):
        # the user's decision arrives mid-draft, after the scan already ran
        review.write_text(review.read_text().replace(
            f"- [ ] approve {target['id']}", f"- [x] approve {target['id']}"))
        return await fake_drafter(item)

    (vault / "Home.md").write_text((vault / "Home.md").read_text() + "\nnudge\n")
    summary = run(make_watcher(vault, drafter=checking_drafter, n_max=3))
    assert summary["approved"] == 1  # applied in-pass, after the scan
    assert run(watcher)["scanned"]   # so the memo was dropped: next pass scans


def test_decision_is_serviced_between_drafting_calls(vault, watcher, make_watcher):
    """A slow drafting phase holds the pass lock for minutes; a checked
    box must land after at most ONE draft call, not at pass end."""
    run(watcher)  # leaves pending proposals
    target = proposals.pending(watcher.proposals_dir)[0]
    review = vault / "Harness/Review.md"
    calls, acted_before_second_draft = [], []

    async def slow_phase_drafter(item):
        calls.append(item["note"])
        if len(calls) == 1:  # the user decides during the first draft
            review.write_text(review.read_text().replace(
                f"- [ ] approve {target['id']}", f"- [x] approve {target['id']}"))
        if len(calls) == 2:  # before the second draft, it must already be applied
            acted_before_second_draft.append(
                f"approve {target['id']}" not in review.read_text())
        return await fake_drafter(item)

    (vault / "Home.md").write_text((vault / "Home.md").read_text() + "\nnudge\n")
    summary = run(make_watcher(vault, drafter=slow_phase_drafter, n_max=3))
    assert summary["approved"] == 1
    assert acted_before_second_draft == [True]


def test_invalid_drafter_output_never_enters_queue(vault, tmp_path, monkeypatch):
    monkeypatch.setattr("harness.watcher.record_dashboard", lambda *a, **k: None)

    async def bad_drafter(item):
        return {"title": "x", "summary": "y",
                "apply": [{"tool": "delete_everything", "args": {}}]}

    w = Watcher(str(vault), proposals_dir=tmp_path / "p2",
                state_path=tmp_path / "s2.json", drafter=bad_drafter, n_max=2)
    summary = run(w)
    assert summary["drafted"] == 0
    assert proposals.pending(w.proposals_dir) == []


def test_proposal_save_validates():
    with pytest.raises(ValueError, match="invalid proposal"):
        proposals.save({"schema_version": 1, "id": "p-nothexdigits"})
