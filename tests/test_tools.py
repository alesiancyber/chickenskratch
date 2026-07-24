"""Smoke tests: every MCP tool exercised against a copy of the test vault.
The `vault` fixture comes from conftest.py."""

import re
import subprocess

import pytest

import obsidian_mcp.vault as vault_mod
from obsidian_mcp import server


def test_list_and_read(vault):
    notes = server.list_notes()["notes"]
    assert "Tasks/Inbox.md" in notes
    assert "Projects/Project Board.md" in notes

    note = server.read_note("Projects/Website Redesign", section="Goals")
    assert "pricing page" in note["content"]
    assert note["frontmatter"]["status"] == "active"


def test_read_note_suggests_on_miss(vault):
    with pytest.raises(vault_mod.VaultError, match="Did you mean"):
        server.read_note("Website Redesign")


def test_search(vault):
    result = server.search_vault("DNS")
    hit_notes = {m["note"] for m in result["matches"]}
    assert "Projects/Project Board.md" in hit_notes
    assert "Meetings/2026-07-15 Sprint Planning.md" in hit_notes

    paged = server.search_vault("e", limit=3)
    assert len(paged["matches"]) == 3 and "next_offset" in paged


def test_create_append_replace(vault):
    server.create_note("Tasks/Vector DBs", "# Vector DBs\n\n## Notes\n\n- start\n")
    with pytest.raises(vault_mod.VaultError, match="already exists"):
        server.create_note("Tasks/Vector DBs")

    server.append_to_note("Tasks/Vector DBs", "- pgvector looks fine", section="Notes")
    content = server.read_note("Tasks/Vector DBs")["content"]
    assert content.index("- start") < content.index("- pgvector looks fine")

    server.replace_section("Projects/Website Redesign", "Status", "Brand guidelines arrived.")
    note = server.read_note("Projects/Website Redesign")
    assert "Brand guidelines arrived." in note["content"]
    assert "Waiting on brand guidelines" not in note["content"]
    assert "Faster load times" in note["content"]  # neighboring section untouched


def test_replace_line(vault):
    server.create_note("Tasks/Cmd", "# Cmd\n\nabove\n\n//azure alert correlation\n\nbelow\n")
    server.replace_line("Tasks/Cmd", "//azure alert correlation",
                        "**//azure alert correlation**\n\n- link one\n- link two")
    content = server.read_note("Tasks/Cmd")["content"]
    assert "- link one\n- link two" in content
    assert content.index("above") < content.index("- link one") < content.index("below")
    assert "\n//azure" not in content  # the command line itself is gone

    with pytest.raises(vault_mod.VaultError, match="not found"):
        server.replace_line("Tasks/Cmd", "//azure alert correlation", "x")
    server.append_to_note("Tasks/Cmd", "dupe\ndupe")
    with pytest.raises(vault_mod.VaultError, match="must be unique"):
        server.replace_line("Tasks/Cmd", "dupe", "x")


def test_frontmatter(vault):
    result = server.update_frontmatter("Tasks/Reading List", "status", "active")
    assert result["frontmatter"] == {"type": "research", "status": "active"}


def test_task_grouping(vault):
    result = server.list_tasks(status="open")
    counts = result["counts_by_type"]
    assert result["filter"] == {"status": "open"}
    assert result["total"] == sum(counts.values())
    assert counts["kanban"] == 4          # board cards are typed, not hidden
    assert counts["meeting"] >= 2         # frontmatter type wins
    assert counts["research"] >= 4
    assert counts["task"] >= 4            # vault.yaml folder→type map (Tasks: task)
    assert counts["deliverable"] >= 2     # Tasks/Owed.md is frontmatter-typed

    kanban_only = server.list_tasks(status="open", task_type="kanban")
    assert kanban_only["total"] == 4
    assert all(t["type"] == "kanban" for t in kanban_only["tasks"])
    assert kanban_only["counts_by_type"] == {"kanban": 4}


def test_tasks(vault):
    open_tasks = server.list_tasks(status="open")
    texts = [t["text"] for t in open_tasks["tasks"]]
    assert "Renew car registration" in texts
    assert all(not t["done"] for t in open_tasks["tasks"])

    due = server.list_tasks(due_before="2026-07-19")
    due_texts = {t["text"] for t in due["tasks"]}
    assert {"Buy a new laptop charger", "Rio: finish DNS migration"} <= due_texts
    assert all(t["due"] < "2026-07-19" for t in due["tasks"])

    server.add_task("Tasks/Inbox", "Renew domain", due="2026-07-25", priority="high", section="Tasks")
    inbox = (vault / "Tasks/Inbox.md").read_text()
    assert "- [ ] Renew domain ⏫ 📅 2026-07-25" in inbox

    server.set_task_status("Tasks/Inbox", "Renew domain")
    inbox = (vault / "Tasks/Inbox.md").read_text()
    assert "- [x] Renew domain ⏫ 📅 2026-07-25 ✅" in inbox

    server.set_task_status("Tasks/Inbox", "Renew domain", done=False)
    assert "- [ ] Renew domain" in (vault / "Tasks/Inbox.md").read_text()


def test_due_on(vault):
    result = server.list_tasks(status="open", due_on="2026-07-18")
    assert all(t["due"] == "2026-07-18" for t in result["tasks"])
    assert "Buy a new laptop charger" in {t["text"] for t in result["tasks"]}
    assert result["filter"]["due_on"] == "2026-07-18"


def test_create_board(vault):
    server.create_board("Projects/Marketing", columns="Ideas, Active, Shipped")
    board = server.read_board("Projects/Marketing")
    assert [c["name"] for c in board["columns"]] == ["Ideas", "Active", "Shipped"]
    server.add_card("Projects/Marketing", "Ideas", "Launch teaser")
    raw = (vault / "Projects/Marketing.md").read_text()
    assert raw.startswith("---\nkanban-plugin: board\nid: b-")  # born stamped

    with pytest.raises(vault_mod.VaultError, match="already exists"):
        server.create_board("Projects/Marketing")


def test_kanban(vault):
    board = server.read_board("Projects/Project Board")
    assert [c["name"] for c in board["columns"]] == ["Backlog", "Doing", "Done"]

    server.add_card("Projects/Project Board", "Backlog", "Draft pricing page")
    server.move_card("Projects/Project Board", "Draft pricing", to_column="Doing")
    board = server.read_board("Projects/Project Board")
    doing = next(c for c in board["columns"] if c["name"] == "Doing")
    moved = next(c for c in doing["cards"] if c["text"] == "Draft pricing page")
    assert moved["done"] is False
    assert moved["id"].startswith("c-")  # stamped at add_card, survives the move

    server.move_card("Projects/Project Board", "DNS", to_column="Done")
    board = server.read_board("Projects/Project Board")
    done = next(c for c in board["columns"] if c["name"] == "Done")
    assert {"text": "Migrate DNS to new registrar", "done": True} in done["cards"]

    raw = (vault / "Projects/Project Board.md").read_text()
    assert raw.startswith("---\nkanban-plugin: board\n---")
    assert "%% kanban:settings" in raw
    assert "**Complete**" in raw


def test_kanban_rewrite_preserves_unmodeled_content(vault):
    board_path = vault / "Projects/Project Board.md"
    raw = board_path.read_text()
    # Enrich the board with everything the harness doesn't model: card
    # sub-content, indented subtasks, column prose, an archive-ish section.
    raw = raw.replace(
        "- [ ] Design new logo",
        "- [ ] Design new logo\n"
        "  Brief: keep the old colors\n"
        "  - [ ] gather references\n"
        "Column note: groom weekly",
    )
    board_path.write_text(raw)

    server.add_card("Projects/Project Board", "Backlog", "Draft pricing page")
    server.move_card("Projects/Project Board", "Design new logo", to_column="Doing")

    after = board_path.read_text()
    assert "  Brief: keep the old colors" in after
    assert "  - [ ] gather references" in after
    assert "Column note: groom weekly" in after
    assert "%% kanban:settings" in after and "**Complete**" in after
    # the sub-content moved with its card
    doing = after.split("## Doing")[1].split("## Done")[0]
    assert "Brief: keep the old colors" in doing

    # indented subtasks are card details, never top-level cards
    board = server.read_board("Projects/Project Board")
    all_cards = [c["text"] for col in board["columns"] for c in col["cards"]]
    assert "gather references" not in all_cards


def test_set_task_status_handles_nonstandard_marks(vault):
    inbox = vault / "Tasks/Inbox.md"
    inbox.write_text(inbox.read_text() + "- [/] Half-finished thing\n")
    server.set_task_status("Tasks/Inbox", "Half-finished thing", done=True)
    text = inbox.read_text()
    assert "- [x] Half-finished thing ✅" in text
    assert "[/]" not in text


def test_daily_and_meeting_notes(vault):
    first = server.open_daily_note("2026-07-17")
    assert first["created"] and first["path"] == "Daily/2026-07-17.md"
    assert not server.open_daily_note("2026-07-17")["created"]

    result = server.create_meeting_note("Design Sync", date="2026-07-17", attendees="Sam, Rio")
    note = server.read_note(result["created"])
    assert note["frontmatter"]["attendees"] == ["Sam", "Rio"]
    assert "**Day:** Friday, 2026-07-17" in note["content"]  # template used, {{day}} filled
    assert "## Topics covered" in note["content"]
    assert "## Action items" in note["content"]


def test_validate_schema_flags_fixture_nonconformance(vault):
    result = server.validate_schema()
    assert result["total_checked"] > 0
    by_note = {r["note"]: r for r in result["nonconforming"]}

    # fixture meeting notes predate stamping: no id => unmarked, flagged
    meeting = by_note["Meetings/2026-07-15 Sprint Planning.md"]
    assert meeting["entity"] == "meeting" and meeting["unmarked"]
    assert any("'id' is a required" in i for i in meeting["issues"])

    # deliverables missing [for:: ...] / due are nonconforming
    owed = by_note["Tasks/Owed.md"]
    assert owed["entity"] == "deliverable"
    assert any(any(i.startswith("for:") for i in t["issues"]) for t in owed["tasks"])

    # unmarked entities sort before any marked-but-drifted ones
    assert result["nonconforming"][0]["unmarked"]
    # Private/ is invisible here too
    assert not any(r["note"].startswith("Private/") for r in result["nonconforming"])


def test_validate_schema_passes_born_stamped_entities(vault):
    server.create_meeting_note("Schema Sync", date="2026-07-17", attendees="Sam")
    result = server.validate_schema(folder="Meetings")
    flagged = {r["note"] for r in result["nonconforming"]}
    assert "Meetings/2026-07-17 Schema Sync.md" not in flagged
    assert "Meetings/2026-07-15 Sprint Planning.md" in flagged  # fixtures still flagged

    note = server.read_note("Meetings/2026-07-17 Schema Sync")
    assert note["frontmatter"]["id"].startswith("m-")


def test_validate_schema_paginates(vault):
    first = server.validate_schema(limit=1)
    assert first["shown"] == 1 and first["next_offset"] == 1
    second = server.validate_schema(limit=1, offset=first["next_offset"])
    assert second["nonconforming"] != first["nonconforming"]


def test_new_tasks_and_cards_born_stamped(vault):
    server.add_task("Tasks/Inbox", "Stamped at birth")
    line = next(ln for ln in (vault / "Tasks/Inbox.md").read_text().splitlines()
                if "Stamped at birth" in ln)
    assert " ^t-" in line

    server.add_card("Projects/Project Board", "Backlog", "Stamped card")
    board = server.read_board("Projects/Project Board")
    backlog = next(c for c in board["columns"] if c["name"] == "Backlog")
    card = next(c for c in backlog["cards"] if c["text"] == "Stamped card")
    assert card["id"].startswith("c-")   # read_board shows clean text + the id


def test_bad_dates_rejected_before_becoming_filenames(vault):
    with pytest.raises(vault_mod.VaultError, match="YYYY-MM-DD"):
        server.open_daily_note("tomorrow")
    with pytest.raises(vault_mod.VaultError, match="YYYY-MM-DD"):
        server.create_meeting_note("Standup", date="next tuesday")
    with pytest.raises(vault_mod.VaultError, match="YYYY-MM-DD"):
        server.open_daily_note("2026-02-30")  # well-formed but not a real date
    assert not (vault / "Daily/tomorrow.md").exists()


def test_snapshot_failure_blocks_mutation_and_reports(vault):
    server.add_task("Tasks/Inbox", "make the repo exist")  # first write inits .git
    before = (vault / "Tasks/Inbox.md").read_text()
    (vault / ".git" / "index.lock").touch()   # simulate a stuck/concurrent git
    (vault / "Manual.md").write_text("uncommitted manual edit\n")
    with pytest.raises(vault_mod.VaultError, match="snapshot failed"):
        server.add_task("Tasks/Inbox", "should not land")
    # snapshot_before couldn't preserve the manual edit, so the write never ran
    assert (vault / "Tasks/Inbox.md").read_text() == before


def test_path_safety(vault):
    with pytest.raises(vault_mod.VaultError, match="escapes"):
        server.read_note("../../etc/passwd")


def test_git_snapshots(vault):
    server.add_task("Tasks/Inbox", "snapshot check")
    log = subprocess.run(
        ["git", "-C", str(vault), "log", "--oneline"], capture_output=True, text=True
    ).stdout
    assert "harness: add task to Tasks/Inbox.md" in log


def test_history_and_revert(vault):
    server.add_task("Tasks/Inbox", "revert me")
    v = vault_mod.get_vault()

    commits = v.history()
    assert commits[0]["message"] == "harness: add task to Tasks/Inbox.md"
    assert commits[0]["files"] == ["Tasks/Inbox.md"]

    ok, err = v.revert(commits[0]["sha"])
    assert ok, err
    assert "revert me" not in (vault / "Tasks/Inbox.md").read_text()
    assert v.history()[0]["message"].startswith("Revert")

    # revert the revert: the task comes back
    ok, _ = v.revert(v.history()[0]["sha"])
    assert ok
    assert "revert me" in (vault / "Tasks/Inbox.md").read_text()


def test_dot_dirs_are_never_notes(vault):
    """Foam/VSCode machinery (.foam templates, .vscode) must be invisible
    to every read path — enumerating dot-dirs by name was a losing game."""
    (vault / ".foam" / "templates").mkdir(parents=True)
    (vault / ".foam" / "templates" / "daily.md").write_text("# a template, not a note\n")
    (vault / ".vscode").mkdir(exist_ok=True)
    (vault / ".vscode" / "notes.md").write_text("machinery\n")
    notes = server.list_notes()["notes"]
    assert not any(n.startswith(".") for n in notes)
    hits = server.search_vault("a template, not a note")["matches"]
    assert hits == []


def test_daily_dir_follows_the_spec(vault):
    (vault / "Harness").mkdir(exist_ok=True)
    (vault / "Harness" / "Spec.md").write_text("---\ndaily_dir: journal\n---\n")
    result = server.open_daily_note()
    assert result["path"].startswith("journal/")
    assert (vault / result["path"]).exists()


def test_add_task_joins_the_list_not_a_new_paragraph(vault):
    """2026-07-23 dogfood: sectionless add_task (the capture-approval
    path) was paragraph-breaking every task into its own island. A task
    appended after a task joins the list; after prose, one blank line."""
    server.create_note("Tasks/Fresh", "# Fresh\n\nsome intro prose\n")
    server.add_task("Tasks/Fresh", "first task")
    server.add_task("Tasks/Fresh", "second task")
    server.add_task("Tasks/Fresh", "third task", due="2026-07-30")
    body = (vault / "Tasks/Fresh.md").read_text()
    assert "prose\n\n- [ ] first task" in body          # prose -> one blank
    assert re.search(r"first task \^t-\w+\n- \[ \] second task", body)   # task -> adjacent
    assert re.search(r"second task \^t-\w+\n- \[ \] third task", body)
    assert "\n\n\n" not in body                          # never a blank run


def test_replace_section_never_accretes_blank_runs(vault):
    """Repeated section rewrites must not stack blank-line envelopes."""
    server.create_note("Tasks/Sections", "# S\n\n## A\n\nold\n\n## B\n\nkeep\n")
    for _ in range(3):
        server.replace_section("Tasks/Sections", "A", "new content")
    body = (vault / "Tasks/Sections.md").read_text()
    assert "\n\n\n" not in body
    assert body.count("new content") == 1
    assert "## B\n\nkeep" in body


def test_archive_done_tasks_tool(vault):
    server.create_note(
        "Tasks/Work",
        "# Work\n\n## Tasks\n\n"
        "- [ ] still open 📅 2026-08-01 ^t-aaaaaaaaaaa1\n"
        "- [x] finished with date ✅ 2026-07-20 ^t-aaaaaaaaaaa2\n"
        "- [x] hand checked in foam ^t-aaaaaaaaaaa3\n")
    result = server.archive_done_tasks("Tasks/Work")
    assert result["archived"] == 2
    work = (vault / "Tasks/Work.md").read_text()
    assert "still open" in work and "[x]" not in work
    assert "\n\n\n" not in work  # removal leaves no blank runs
    archive = (vault / "Tasks/Archive.md").read_text()
    assert "- [x] finished with date ✅ 2026-07-20 ^t-aaaaaaaaaaa2" in archive  # byte-identical
    import datetime as dt
    today = dt.date.today().isoformat()
    assert f"- [x] hand checked in foam ✅ {today} ^t-aaaaaaaaaaa3" in archive  # stamped
    # idempotent: nothing left to archive
    assert server.archive_done_tasks("Tasks/Work")["archived"] == 0
    # archiving the archive is a no-op, never recursion
    assert server.archive_done_tasks("Tasks/Archive")["archived"] == 0
