"""Grouping G1: structural candidate
building, partition integrity, byte-identical section rewrites, and the
watcher pipeline end to end with a scripted grouping drafter."""

import asyncio

import pytest

from harness import grouping, proposals
from obsidian_mcp.vault import Vault

STAMPED_LIST = """# Braindump

## Tasks

- [ ] Tune Acme detection rules ^t-aaaaaaaaaaa1
- [ ] Report detection coverage to Acme ^t-aaaaaaaaaaa2
- [ ] Renew the mail server cert 📅 2026-08-01 ^t-aaaaaaaaaaa3
- [ ] Rotate the DKIM keys ⏫ ^t-aaaaaaaaaaa4
"""

IDS = ["t-aaaaaaaaaaa1", "t-aaaaaaaaaaa2", "t-aaaaaaaaaaa3", "t-aaaaaaaaaaa4"]


# ---- candidate builder (structural rules, no LLM) ----

def test_stamped_flat_list_is_a_candidate():
    cand = grouping.note_candidate(STAMPED_LIST)
    assert cand is not None
    assert cand["section"] == "Tasks" and cand["level"] == 2
    assert [t.block_id for t in cand["tasks"]] == IDS


def test_unstamped_tasks_are_not_candidates():
    text = STAMPED_LIST.replace(" ^t-aaaaaaaaaaa3", "")
    assert grouping.note_candidate(text) is None


def test_short_lists_are_not_candidates():
    text = "\n".join(STAMPED_LIST.splitlines()[:6]) + "\n"  # only 3 tasks
    assert grouping.note_candidate(text) is None


def test_prose_in_section_disqualifies():
    text = STAMPED_LIST.replace(
        "- [ ] Rotate the DKIM keys ⏫ ^t-aaaaaaaaaaa4",
        "Some prose note between tasks.\n- [ ] Rotate the DKIM keys ⏫ ^t-aaaaaaaaaaa4",
    )
    assert grouping.note_candidate(text) is None


def test_subheadings_mean_already_organized():
    text = STAMPED_LIST.replace(
        "- [ ] Renew the mail server cert 📅 2026-08-01 ^t-aaaaaaaaaaa3",
        "### Infra\n- [ ] Renew the mail server cert 📅 2026-08-01 ^t-aaaaaaaaaaa3",
    )
    assert grouping.note_candidate(text) is None


def test_nested_tasks_disqualify():
    text = STAMPED_LIST.replace(
        "- [ ] Report detection coverage to Acme ^t-aaaaaaaaaaa2",
        "  - [ ] Report detection coverage to Acme ^t-aaaaaaaaaaa2",
    )
    assert grouping.note_candidate(text) is None


def test_kanban_boards_are_never_candidates():
    # The customer-boundary regression at the structural layer: a board
    # holding two projects' similarly-worded cards yields no candidate.
    board = (
        "---\nkanban-plugin: board\n---\n\n## Acme\n\n"
        "- [ ] Tune detection ^t-aaaaaaaaaaa1\n- [ ] Report coverage ^t-aaaaaaaaaaa2\n"
        "- [ ] Review alerts ^t-aaaaaaaaaaa3\n- [ ] Ship dashboard ^t-aaaaaaaaaaa4\n"
    )
    assert grouping.note_candidate(board) is None


# ---- partition integrity ----

def test_valid_partition_passes():
    groups = [
        {"name": "Acme", "member_ids": IDS[:2]},
        {"name": "Mail infra", "member_ids": IDS[2:]},
    ]
    assert grouping.validate_partition(groups, IDS) == []


@pytest.mark.parametrize("groups,needle", [
    ([{"name": "A", "member_ids": IDS[:2]}], "dropped"),                       # dropped ids
    ([{"name": "A", "member_ids": IDS + ["t-ffffffffffff"]}], "invented"),     # invented id
    ([{"name": "A", "member_ids": IDS}, {"name": "B", "member_ids": [IDS[0]]}], "more than one"),
    ([{"name": "", "member_ids": IDS}], "empty name"),
    ([{"name": "A", "member_ids": IDS[:2]}, {"name": "a", "member_ids": IDS[2:]}], "duplicate"),
])
def test_broken_partitions_are_named(groups, needle):
    assert any(needle in i for i in grouping.validate_partition(groups, IDS))


def test_render_grouped_keeps_lines_byte_identical():
    cand = grouping.note_candidate(STAMPED_LIST)
    line_by_id = {t.block_id: t.raw for t in cand["tasks"]}
    groups = [
        {"name": "Acme", "member_ids": IDS[:2]},
        {"name": "Mail infra", "member_ids": IDS[2:]},
    ]
    out = grouping.render_grouped(cand["level"], groups, line_by_id)
    assert "### Acme" in out and "### Mail infra" in out
    for t in cand["tasks"]:
        assert t.raw in out.splitlines()  # byte-identical, ids included


# ---- watcher pipeline ----

async def fake_schema_drafter(item):
    return {
        "title": f"Normalize {item['note']}",
        "summary": "stamp",
        "apply": [{"tool": "update_frontmatter",
                   "args": {"path": item["note"], "key": "id", "value": item["new_id"]}}],
    }


async def fake_grouping_drafter(item):
    ids_ = [t.block_id for t in item["tasks"]]
    return {"groups": [
        {"name": "Acme", "member_ids": ids_[:2]},
        {"name": "Mail infra", "member_ids": ids_[2:]},
    ]}


@pytest.fixture()
def vault(vault):
    """Conftest vault + the planted flat list, committed as the baseline.
    Auto-apply trust revoked: this suite tests the checkbox path."""
    (vault / "Tasks" / "Braindump.md").write_text(STAMPED_LIST, encoding="utf-8")
    (vault / "Harness").mkdir(exist_ok=True)
    (vault / "Harness/Spec.md").write_text("---\nauto_apply: []\n---\n\n# Vault Spec\n")
    Vault(vault).commit_if_dirty("baseline")
    return vault


@pytest.fixture()
def make_watcher(make_watcher):
    """Conftest factory with this suite's scripted drafters as defaults."""
    def make(vault_path, **kw):
        kw.setdefault("drafter", fake_schema_drafter)
        kw.setdefault("grouping_drafter", fake_grouping_drafter)
        kw.setdefault("n_max", 20)  # fixture has ~11 schema items; grouping drafts after them
        return make_watcher(vault_path, **kw)
    return make


def run(w):
    return asyncio.run(w.run_once())


def grouping_rows(w):
    return [p for p in proposals.pending(w.proposals_dir) if p["kind"] == "grouping"]


def test_grouping_proposal_drafted_and_applied(vault, make_watcher):
    w = make_watcher(vault)
    run(w)
    rows = grouping_rows(w)
    assert len(rows) == 1
    p = rows[0]
    assert p["drafted_against"]["note"] == "Tasks/Braindump.md"
    assert p["drafted_against"]["entity_ids"] == IDS
    assert p["apply"][0]["tool"] == "replace_section"

    review = vault / "Harness/Review.md"
    assert p["title"] in review.read_text()
    review.write_text(review.read_text().replace(
        f"- [ ] approve {p['id']}", f"- [x] approve {p['id']}"))
    summary = run(w)

    assert summary["approved"] == 1
    after = (vault / "Tasks/Braindump.md").read_text()
    assert "### Acme" in after and "### Mail infra" in after
    for line in STAMPED_LIST.splitlines():
        if line.startswith("- [ ]"):
            assert line in after.splitlines()  # tasks byte-identical after regroup


def test_grouped_note_is_not_recandidated(vault, make_watcher):
    w = make_watcher(vault)
    run(w)
    p = grouping_rows(w)[0]
    review = vault / "Harness/Review.md"
    review.write_text(review.read_text().replace(
        f"- [ ] approve {p['id']}", f"- [x] approve {p['id']}"))
    run(w)
    # the rewritten section now has sub-headings -> structurally not a
    # flat list -> the next scan proposes nothing for it
    run(w)
    assert grouping_rows(w) == []


def test_broken_drafter_partition_never_becomes_a_proposal(vault, make_watcher):
    async def inventing_drafter(item):
        return {"groups": [{"name": "A", "member_ids": ["t-ffffffffffff"]}]}

    w = make_watcher(vault, grouping_drafter=inventing_drafter)
    run(w)
    assert grouping_rows(w) == []


def test_single_group_reply_is_skipped(vault, make_watcher):
    async def one_group(item):
        return {"groups": [{"name": "Everything", "member_ids": [t.block_id for t in item["tasks"]]}]}

    w = make_watcher(vault, grouping_drafter=one_group)
    run(w)
    assert grouping_rows(w) == []


def test_grouping_flag_disables_the_builder(vault, make_watcher):
    w = make_watcher(vault, disabled=("grouping",))
    run(w)
    assert grouping_rows(w) == []
