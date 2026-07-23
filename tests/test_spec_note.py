"""The vault describes itself: Harness/Spec.md frontmatter is the live
spec (seeded once from config/vault.yaml), and the spec_drift processor
proposes map updates when new top-level folders appear."""

import asyncio

import pytest

from harness import processors as processors_module
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


async def stamp_drafter(item):
    """Harmless plan so passes with the full default registry stay offline."""
    return {"title": "t", "summary": "s",
            "apply": [{"tool": "update_frontmatter",
                       "args": {"path": item["note"], "key": "id", "value": item["new_id"]}}]}


def full_registry_watcher(make_watcher, vault, **kw):
    """A watcher running the DEFAULT registry (needed to observe Spec.md
    disabled_processors) with every worker faked to a no-op."""
    async def no_tasks(text, today):
        return []
    async def no_groups(item):
        return {"groups": []}
    return make_watcher(vault, drafter=stamp_drafter, capture_extractor=no_tasks,
                        grouping_drafter=no_groups, **kw)


def test_spec_note_drives_engine_knobs_per_pass(vault, make_watcher, monkeypatch):
    """worker / n_max / disabled_processors are vault-side choices: a
    Spec.md edit reconfigures the next pass, no restart. Explicit
    constructor args (CLI flags, tests) still pin their knob."""
    w = full_registry_watcher(make_watcher, vault)
    run(w)  # seeds Spec.md
    fm, body = md.split_frontmatter(spec_path(vault).read_text())
    assert fm["n_max"] == 5 and fm["disabled_processors"] == []
    fm["n_max"] = 2
    fm["disabled_processors"] = ["grouping", "schema_fix"]
    fm["worker"] = "spec-picked"
    spec_path(vault).write_text(md.join_frontmatter(fm, body))

    applied = []
    monkeypatch.setattr("harness.watcher.apply_worker_profile", lambda name: applied.append(name))
    run(w)
    assert w.n_max == 2
    assert [p.name for p in w.processors] == ["capture", "inline_command", "inbox_extract", "spec_drift"]
    assert applied == ["spec-picked"]

    # a pinned knob wins over the note
    w2 = full_registry_watcher(make_watcher, vault, n_max=9,
                               state_path=vault / "s2.json", proposals_dir=vault / "p2")
    run(w2)
    assert w2.n_max == 9


def test_spec_note_bad_processor_name_degrades_not_crashes(vault, make_watcher, capsys):
    w = full_registry_watcher(make_watcher, vault)
    run(w)
    fm, body = md.split_frontmatter(spec_path(vault).read_text())
    fm["disabled_processors"] = ["no_such_processor"]
    spec_path(vault).write_text(md.join_frontmatter(fm, body))
    run(w)  # pass completes on the previous processor set
    assert "no_such_processor" in capsys.readouterr().out
    assert len(w.processors) == len(processors_module.REGISTRY)


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
