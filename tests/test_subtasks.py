"""Subtasks are indentation: the grammar resolves depth and parent from
the indent stack, the record writes a worker's depth back as
indentation, and the dashboard makes a subtask ride with its parent —
one task, one row, still."""

import datetime as dt

import pytest

from chickenskratch import processors
from chickenskratch import today as today_mod
from chickenskratch_vault import server
from chickenskratch_vault import tasks as tk
from chickenskratch_vault.workers import _shape_tasks
from tests.test_today import _vault_and_cfg, day

# ---- grammar ----


def test_iter_tasks_resolves_depth_and_parent_from_indentation():
    doc = (
        "# Plan\n\n"
        "- [ ] migrate the wiki ^t-aaaaaaaaaaaa\n"
        "  - [ ] export the space\n"
        "  - some note between the steps\n"
        "  - [ ] import it\n"
        "\t- [ ] tab-indented: still deeper than two spaces\n"
        "- [ ] ping dana\n"
        "\n"
        "A paragraph ends the list.\n"
        "  - [ ] orphan: indented, but nothing above to hang from\n"
    )
    tasks = list(tk.iter_tasks(doc))
    by_text = {t.text.split(":")[0]: t for t in tasks}
    top = by_text["migrate the wiki"]
    assert (top.depth, top.indent, top.parent_line) == (0, 0, None)
    export = by_text["export the space"]
    assert export.depth == 1 and export.parent_line == top.line_no
    assert export.parent_id == "t-aaaaaaaaaaaa" and export.parent_text == "migrate the wiki"
    assert by_text["import it"].depth == 1                # a bullet between siblings is content
    tab = by_text["tab-indented"]
    assert tab.depth == 2 and tab.parent_text == "import it"   # a tab is wider than two spaces
    assert by_text["ping dana"].depth == 0
    assert by_text["orphan"].depth == 0                   # surfaces on its own, never vanishes


def test_a_line_on_its_own_knows_only_its_indent():
    t = tk.parse_task_line("    - [ ] deep")
    assert (t.indent, t.depth, t.parent_text) == (4, 0, None)


def test_to_dict_carries_nesting_only_for_subtasks():
    doc = "- [ ] parent ^t-bbbbbbbbbbbb\n  - [ ] child\n"
    parent, child = (t.to_dict("N.md") for t in tk.iter_tasks(doc))
    assert "depth" not in parent and "parent" not in parent
    assert child["depth"] == 1 and child["parent"] == "t-bbbbbbbbbbbb"
    assert child["parent_text"] == "parent"


def test_normalize_depths_makes_nesting_expressible():
    assert tk.normalize_depths([]) == []
    assert tk.normalize_depths([1, 2, 0, 3, 1]) == [0, 1, 0, 1, 1]   # first is 0; no skipped level
    assert tk.normalize_depths([0, 1, 2, 2, 0]) == [0, 1, 2, 2, 0]
    assert tk.normalize_depths([None, -1, 1]) == [0, 0, 1]
    assert tk.indent_line("- [ ] x", 2) == "    - [ ] x"


# ---- the worker contract ----


def test_shaped_tasks_carry_a_well_formed_depth():
    raw = [{"text": "a", "depth": 2}, {"text": "b", "depth": 1},
           {"text": "c"}, {"text": "d", "depth": None}, {"text": "e", "depth": True}]
    assert [t["depth"] for t in _shape_tasks(raw)] == [0, 1, 0, 0, 0]


def test_record_lines_write_depth_as_indentation():
    lines = processors._record_task_lines([
        {"text": "migrate the wiki", "depth": 0},
        {"text": "export the space", "depth": 1},
        {"text": "import it", "depth": 1},
        {"text": "ping dana", "depth": 0},
    ])
    assert [ln[:len(ln) - len(ln.lstrip())] for ln in lines] == ["", "  ", "  ", ""]
    assert lines[1].lstrip().startswith("- [ ] export the space")
    # and the filed lines parse back to the same tree
    assert [t.depth for t in tk.iter_tasks("\n".join(lines))] == [0, 1, 1, 0]


def test_an_owed_step_split_from_its_parent_starts_at_the_top():
    """The own/owed split files a child away from its parent: in the
    waiting list it is a top-level item, never a dangling indent."""
    tasks = [{"text": "migrate the wiki", "depth": 0},
             {"text": "send the export", "depth": 1, "owed_by": "Nadia"}]
    own, owed = processors._split_owed(tasks)
    assert processors._record_task_lines(owed)[0].startswith("- [ ] send the export 👤 Nadia")
    assert processors._record_task_lines(own)[0].startswith("- [ ] migrate the wiki")


# ---- the dashboard ----


@pytest.fixture()
def vault(gitvault):
    (gitvault / "Harness").mkdir(exist_ok=True)
    (gitvault / "Harness/Spec.md").write_text(
        "---\npolicies: {capture: ask}\n---\n\n# Vault Spec\n")
    (gitvault / "Meetings" / f"{day(-20)} Kickoff.md").write_text(
        "# Kickoff\n\n## Action items\n\n"
        f"- [ ] migrate the wiki ⏳ {day(0)} ➕ {day(-20)} ^t-cccccccccccc\n"
        "  - [ ] export the space\n"
        f"  - [ ] import it 📅 {day(1)}\n"
        f"  - [x] pick a target folder ✅ {day(-1)}\n"
        "  - [ ] ask nadia for the export 👤 Nadia\n"
        f"- [ ] write the runbook ➕ {day(-20)} ^t-dddddddddddd\n"
        "  - [ ] outline it\n"
        "  - [ ] draft it\n"
    )
    return gitvault


def model(vault):
    v, cfg = _vault_and_cfg(vault)
    return today_mod.collect(v, cfg, 0)


def test_a_planned_parent_carries_its_open_undated_subtasks(vault):
    m = model(vault)
    (row,) = [r for r in m["planned"] if r["text"] == "migrate the wiki"]
    assert [s["text"] for s in row["subtasks"]] == [
        "export the space", "ask nadia for the export"]   # not the dated one, not the done one
    assert row["subtasks_done"] == 1 and row["subtasks_total"] == 4
    assert row["subtasks"][1]["owner"] == "Nadia"        # somebody else's debt, marked as such
    assert "parent" not in row


def test_a_dated_subtask_has_its_own_row_labeled_with_its_parent(vault):
    m = model(vault)
    (row,) = [r for r in m["due"]["soon"] if r["text"] == "import it"]
    assert row["parent"] == "migrate the wiki"
    assert "subtasks" not in row
    # and nowhere else: one task, one row
    assert all(s["text"] != "import it"
               for r in m["planned"] for s in r.get("subtasks", []))


def test_undated_subtasks_ride_with_their_parent_in_the_backlog_and_stranded(vault):
    m = model(vault)
    backlog = {r["text"]: r for r in m["backlog"]}
    assert "outline it" not in backlog and "draft it" not in backlog
    assert [s["text"] for s in backlog["write the runbook"]["subtasks"]] == ["outline it", "draft it"]
    stranded = {r["text"]: r for r in m["stranded"]}
    assert "outline it" not in stranded
    assert stranded["write the runbook"]["subtasks_total"] == 2
    # a group offer counts the parent once, never its children
    assert all(t["text"] not in ("outline it", "draft it")
               for g in m["groups"] for t in g["tasks"])


def test_an_owed_subtask_waits_like_any_owed_item(vault):
    m = model(vault)
    (row,) = [r for r in m["waiting"] if r["text"] == "ask nadia for the export"]
    assert row["parent"] == "migrate the wiki" and row["owner"] == "Nadia"


def test_an_indented_task_with_no_parent_still_surfaces(vault):
    (vault / "Tasks/Loose.md").write_text("- groceries\n  - [ ] milk\n")
    assert "milk" in {r["text"] for r in model(vault)["backlog"]}


# ---- the tool ----


def test_add_task_under_nests_beneath_the_parents_last_subtask(vault):
    rel = f"Meetings/{day(-20)} Kickoff.md"
    server.add_task(rel, "verify the links", under="write the runbook")
    lines = (vault / rel).read_text().splitlines()
    i = next(i for i, ln in enumerate(lines) if "verify the links" in ln)
    assert lines[i].startswith("  - [ ] verify the links") and " ^t-" in lines[i]
    assert lines[i - 1].startswith("  - [ ] draft it")     # after the existing children
    t = next(t for t in tk.iter_tasks("\n".join(lines)) if t.text == "verify the links")
    assert t.depth == 1 and t.parent_text == "write the runbook"


def test_add_task_under_a_childless_parent_indents_one_level(vault):
    (vault / "Tasks/Solo.md").write_text("- [ ] plan the trip\n\n- [ ] unrelated\n")
    server.add_task("Tasks/Solo", "book flights", under="plan the trip")
    text = (vault / "Tasks/Solo.md").read_text()
    assert text.startswith("- [ ] plan the trip\n  - [ ] book flights")
    assert "\n\n- [ ] unrelated\n" in text                  # the blank separator stays put


def test_completing_a_parent_leaves_its_subtasks_alone(vault):
    rel = f"Meetings/{day(-20)} Kickoff.md"
    server.set_task_status(rel, "write the runbook")
    text = (vault / rel).read_text()
    assert "- [x] write the runbook" in text
    assert "  - [ ] outline it" in text and "  - [ ] draft it" in text


def test_archive_moves_a_done_parent_with_its_subtasks(vault):
    (vault / "Tasks/Done.md").write_text(
        f"- [x] shipped ✅ {day(-1)}\n  - [x] step ✅ {day(-1)}\n  - [ ] leftover\n- [ ] keep\n")
    result = server.archive_done_tasks("Tasks/Done", archive_path="Tasks/Archive")
    assert result["archived"] == 1                          # the child is not archived twice
    assert (vault / "Tasks/Done.md").read_text() == "- [ ] keep\n"
    assert "  - [ ] leftover" in (vault / "Tasks/Archive.md").read_text()


def test_today_stays_deterministic_with_nesting(vault):
    assert model(vault) == model(vault)
    assert isinstance(dt.date.today(), dt.date)
