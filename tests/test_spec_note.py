"""The vault describes itself: Harness/Spec.md frontmatter is the live
spec (seeded once from config/vault.yaml), and the spec_drift processor
proposes map updates when new top-level folders appear."""

import asyncio

import pytest

from harness import proposals
from harness.processors import SPEC_DRIFT
from obsidian_mcp import markdown as md
from obsidian_mcp import vaultconfig as vc


@pytest.fixture()
def vault(gitvault):
    return gitvault


async def no_worker(item):
    raise AssertionError("no worker call expected in these tests")


def run(w):
    return asyncio.run(w.run_once())


def spec_path(vault):
    return vault / vc.SPEC_REL


def test_first_pass_seeds_spec_note_once(vault, make_watcher):
    w = make_watcher(vault, drafter=no_worker, n_max=0)
    run(w)
    fm, body = md.split_frontmatter(spec_path(vault).read_text())
    assert fm["folders"]["Tasks"] == "task"
    assert fm["capture_inbox"] == "Tasks/Inbox.md"
    assert "yours" in body  # seeded once, then user-owned
    # user edits survive every subsequent pass — never regenerated
    edited = spec_path(vault).read_text().replace("# Vault Spec", "# Vault Spec (mine)")
    spec_path(vault).write_text(edited)
    run(w)
    assert "# Vault Spec (mine)" in spec_path(vault).read_text()


def test_spec_note_overrides_are_live_per_pass(vault, make_watcher):
    w = make_watcher(vault, drafter=no_worker, n_max=0)
    run(w)
    fm, body = md.split_frontmatter(spec_path(vault).read_text())
    fm["waiting_inbox"] = "Tasks/Blocked.md"
    fm["exclude"] = ["Private", "Meetings"]
    spec_path(vault).write_text(md.join_frontmatter(fm, body))

    cfg = vc.get_config(str(vault))
    assert cfg.waiting_inbox == "Tasks/Blocked.md"
    assert cfg.exclude == ("Private", "Meetings")
    run(w)
    assert w.waiting_inbox == "Tasks/Blocked.md"  # engine picked it up, no restart


def test_new_folder_drafts_a_spec_proposal_and_approval_updates_the_note(vault, make_watcher):
    w = make_watcher(vault, drafter=no_worker, registry=(SPEC_DRIFT,))
    run(w)  # seeds Spec.md; no unknown folders yet
    assert proposals.pending(w.proposals_dir) == []

    (vault / "Detections").mkdir()
    (vault / "Detections/suspicious-logins.md").write_text("# Sigma rule idea\n")
    run(w)
    (p,) = proposals.pending(w.proposals_dir)
    assert p["kind"] == "spec_drift"
    assert "Detections" in p["title"]
    assert p["apply"][0]["tool"] == "update_frontmatter"

    review = vault / "Harness/Review.md"
    review.write_text(review.read_text().replace(
        f"- [ ] approve {p['id']}", f"- [x] approve {p['id']}"))
    summary = run(w)
    assert summary["approved"] == 1
    fm, _ = md.split_frontmatter(spec_path(vault).read_text())
    assert fm["folders"]["Detections"] == "other"
    assert fm["folders"]["Tasks"] == "task"  # existing map preserved
    assert vc.get_config(str(vault)).folder_types["Detections"] == "other"
    # spec now knows the folder -> no re-draft
    run(w)
    assert proposals.pending(w.proposals_dir) == []


def test_empty_or_known_folders_are_not_drift(vault, make_watcher):
    w = make_watcher(vault, drafter=no_worker, registry=(SPEC_DRIFT,))
    run(w)
    (vault / "Scaffolding").mkdir()  # no notes inside: not drift
    run(w)
    assert proposals.pending(w.proposals_dir) == []
