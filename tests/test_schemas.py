"""Entity schema validation: note frontmatter,
task/card/deliverable lines, and the research Sources requirement."""

import datetime as dt

from chickenskratch_vault import ids, schemas, tasks


def good_meeting_fm():
    return {
        "id": ids.stamp_id("m"),
        "type": "meeting",
        "date": "2026-07-17",
        "attendees": ["Sam", "Rio"],
    }


def test_all_core_schemas_load():
    assert {"task", "deliverable", "card", "meeting", "research", "kanban"} <= set(
        schemas.load_schemas()
    )


def test_meeting_frontmatter():
    assert schemas.validate_frontmatter("meeting", good_meeting_fm()) == []
    issues = schemas.validate_frontmatter("meeting", {"type": "meeting"})
    assert any("'id' is a required" in i for i in issues)
    bad = good_meeting_fm() | {"attendees": "Sam"}
    assert any(i.startswith("attendees:") for i in schemas.validate_frontmatter("meeting", bad))


def test_yaml_date_objects_normalized():
    # YAML parses `date: 2026-07-17` into datetime.date; schema still passes
    fm = good_meeting_fm() | {"date": dt.date(2026, 7, 17)}
    assert schemas.validate_frontmatter("meeting", fm) == []


def test_types_without_note_schema_pass():
    assert schemas.validate_frontmatter("daily", {}) == []
    assert schemas.validate_frontmatter("projects", {"anything": 1}) == []
    assert schemas.validate_frontmatter("task", {}) == []   # line type: no fm schema


def test_research_frontmatter_and_body():
    fm = {"id": ids.stamp_id("r"), "type": "research", "status": "active"}
    assert schemas.validate_frontmatter("research", fm) == []
    assert any("status:" in i
               for i in schemas.validate_frontmatter("research", fm | {"status": "someday"}))
    assert schemas.validate_research_body("# T\n\n## Sources\n\n- https://x\n") == []
    assert any("Sources" in i for i in schemas.validate_research_body("# T\n\n## Notes\n"))


def test_task_line_validation():
    stamped = tasks.parse_task_line(f"- [ ] Ship it 📅 2026-07-25 ^{ids.stamp_id('t')}")
    assert schemas.validate_line(stamped, "task") == []
    unstamped = tasks.parse_task_line("- [ ] Ship it")
    assert any(i.startswith("id:") for i in schemas.validate_line(unstamped, "task"))


def test_card_lines_use_card_schema():
    t_id = tasks.parse_task_line(f"- [ ] A card ^{ids.stamp_id('t')}")
    assert any("id:" in i for i in schemas.validate_line(t_id, "kanban"))  # wrong kind prefix
    c_id = tasks.parse_task_line(f"- [ ] A card ^{ids.stamp_id('c')}")
    assert schemas.validate_line(c_id, "kanban") == []


def test_deliverable_requires_for_and_due():
    plain = tasks.parse_task_line(f"- [ ] Send report 📅 2026-07-24 ^{ids.stamp_id('t')}")
    issues = schemas.validate_line(plain, "deliverable")
    assert any(i.startswith("for:") for i in issues)
    # the identical line is a perfectly fine plain task
    assert schemas.validate_line(plain, "task") == []

    full = tasks.parse_task_line(
        f"- [ ] Send report [for:: Dana] 📅 2026-07-24 ^{ids.stamp_id('t')}"
    )
    assert schemas.validate_line(full, "deliverable") == []

    undated = tasks.parse_task_line(f"- [ ] Send report [for:: Dana] ^{ids.stamp_id('t')}")
    assert any(i.startswith("due:") for i in schemas.validate_line(undated, "deliverable"))
