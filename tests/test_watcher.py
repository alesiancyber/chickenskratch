"""The review-dashboard pipeline end to end (DESIGN-review-dashboard.md):
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
    (gitvault / "Harness/Spec.md").write_text("---\nauto_apply: []\n---\n\n# Vault Spec\n")
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
