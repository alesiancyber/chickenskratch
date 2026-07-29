"""The 👤 owner field — owed-ness as grammar, not location. One field in
the same emoji shape as the dates; it replaces "a task is owed because
it sits in Tasks/Waiting.md" (dumps-become-records, phase 1)."""

from tezcat_vault import tasks as tk


def test_owner_round_trips_through_format_and_parse():
    line = tk.format_task("pipeline costs", owner="Nadia",
                          block_id="t-abc123def456", created="2026-07-28")
    assert line == "- [ ] pipeline costs 👤 Nadia ➕ 2026-07-28 ^t-abc123def456"
    t = tk.parse_task_line(line)
    assert t.owner == "Nadia" and t.text == "pipeline costs"
    assert t.created == "2026-07-28" and t.block_id == "t-abc123def456"


def test_owner_is_free_text_up_to_the_next_field():
    t = tk.parse_task_line(
        "- [ ] send the costs 👤 the SOC team ⏳ 2026-08-02 ➕ 2026-07-28 ^t-9f")
    assert t.owner == "the SOC team"
    assert t.text == "send the costs" and t.scheduled == "2026-08-02"


def test_ownerless_tasks_parse_owner_none():
    assert tk.parse_task_line("- [ ] plain task 📅 2026-08-01").owner is None


def test_owner_survives_a_reschedule():
    """Snoozing an owed item is set_line_scheduled — the ⏳ verb must
    slot its date without disturbing the 👤 (or anything else)."""
    line = tk.format_task("pipeline costs", owner="Nadia",
                          block_id="t-abc123def456", created="2026-07-28")
    moved = tk.set_line_scheduled(line, "2026-08-04")
    assert moved == ("- [ ] pipeline costs 👤 Nadia ⏳ 2026-08-04 "
                     "➕ 2026-07-28 ^t-abc123def456")
    assert tk.parse_task_line(moved).owner == "Nadia"


def test_owner_rides_in_to_dict():
    t = tk.parse_task_line("- [ ] pipeline costs 👤 Nadia")
    assert t.to_dict("Meetings/x.md")["owner"] == "Nadia"
