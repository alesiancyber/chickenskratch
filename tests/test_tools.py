"""Smoke tests: every MCP tool exercised against a copy of the test vault."""

import shutil
import subprocess
from pathlib import Path

import pytest

import obsidian_mcp.vault as vault_mod
from obsidian_mcp import server

TESTVAULT = Path(__file__).resolve().parents[1] / "testvault"


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    shutil.copytree(TESTVAULT, root, ignore=shutil.ignore_patterns(".git"))
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(root))
    monkeypatch.setattr(vault_mod, "_vault", None)
    return root


def test_list_and_read(vault):
    notes = server.list_notes()["notes"]
    assert "Tasks/Inbox.md" in notes
    assert "Boards/Project Board.md" in notes

    note = server.read_note("Projects/Website Redesign", section="Goals")
    assert "pricing page" in note["content"]
    assert note["frontmatter"]["status"] == "active"


def test_read_note_suggests_on_miss(vault):
    with pytest.raises(vault_mod.VaultError, match="Did you mean"):
        server.read_note("Website Redesign")


def test_search(vault):
    result = server.search_vault("DNS")
    hit_notes = {m["note"] for m in result["matches"]}
    assert "Boards/Project Board.md" in hit_notes
    assert "Meetings/2026-07-15 Sprint Planning.md" in hit_notes

    paged = server.search_vault("e", limit=3)
    assert len(paged["matches"]) == 3 and "next_offset" in paged


def test_create_append_replace(vault):
    server.create_note("Research/Vector DBs", "# Vector DBs\n\n## Notes\n\n- start\n")
    with pytest.raises(vault_mod.VaultError, match="already exists"):
        server.create_note("Research/Vector DBs")

    server.append_to_note("Research/Vector DBs", "- pgvector looks fine", section="Notes")
    content = server.read_note("Research/Vector DBs")["content"]
    assert content.index("- start") < content.index("- pgvector looks fine")

    server.replace_section("Projects/Website Redesign", "Status", "Brand guidelines arrived.")
    note = server.read_note("Projects/Website Redesign")
    assert "Brand guidelines arrived." in note["content"]
    assert "Waiting on brand guidelines" not in note["content"]
    assert "Faster load times" in note["content"]  # neighboring section untouched


def test_frontmatter(vault):
    result = server.update_frontmatter("Research/Reading List", "status", "active")
    assert result["frontmatter"] == {"type": "research", "status": "active"}


def test_task_grouping(vault):
    result = server.list_tasks(status="open")
    counts = result["counts_by_type"]
    assert result["filter"] == {"status": "open"}
    assert result["total"] == sum(counts.values())
    assert counts["kanban"] == 4          # board cards are typed, not hidden
    assert counts["meeting"] >= 2         # frontmatter type wins
    assert counts["research"] >= 4
    assert counts["tasks"] >= 4           # folder fallback

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
    server.create_board("Boards/Marketing", columns="Ideas, Active, Shipped")
    board = server.read_board("Boards/Marketing")
    assert [c["name"] for c in board["columns"]] == ["Ideas", "Active", "Shipped"]
    server.add_card("Boards/Marketing", "Ideas", "Launch teaser")
    assert (vault / "Boards/Marketing.md").read_text().startswith("---\nkanban-plugin: board\n---")

    with pytest.raises(vault_mod.VaultError, match="already exists"):
        server.create_board("Boards/Marketing")


def test_kanban(vault):
    board = server.read_board("Boards/Project Board")
    assert [c["name"] for c in board["columns"]] == ["Backlog", "Doing", "Done"]

    server.add_card("Boards/Project Board", "Backlog", "Draft pricing page")
    server.move_card("Boards/Project Board", "Draft pricing", to_column="Doing")
    board = server.read_board("Boards/Project Board")
    doing = next(c for c in board["columns"] if c["name"] == "Doing")
    assert {"text": "Draft pricing page", "done": False} in doing["cards"]

    server.move_card("Boards/Project Board", "DNS", to_column="Done")
    board = server.read_board("Boards/Project Board")
    done = next(c for c in board["columns"] if c["name"] == "Done")
    assert {"text": "Migrate DNS to new registrar", "done": True} in done["cards"]

    raw = (vault / "Boards/Project Board.md").read_text()
    assert raw.startswith("---\nkanban-plugin: board\n---")
    assert "%% kanban:settings" in raw
    assert "**Complete**" in raw


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


def test_path_safety(vault):
    with pytest.raises(vault_mod.VaultError, match="escapes"):
        server.read_note("../../etc/passwd")


def test_git_snapshots(vault):
    server.add_task("Tasks/Inbox", "snapshot check")
    log = subprocess.run(
        ["git", "-C", str(vault), "log", "--oneline"], capture_output=True, text=True
    ).stdout
    assert "harness: append to Tasks/Inbox.md" in log


def test_history_and_revert(vault):
    server.add_task("Tasks/Inbox", "revert me")
    v = vault_mod.get_vault()

    commits = v.history()
    assert commits[0]["message"] == "harness: append to Tasks/Inbox.md"
    assert commits[0]["files"] == ["Tasks/Inbox.md"]

    ok, err = v.revert(commits[0]["sha"])
    assert ok, err
    assert "revert me" not in (vault / "Tasks/Inbox.md").read_text()
    assert v.history()[0]["message"].startswith("Revert")

    # revert the revert: the task comes back
    ok, _ = v.revert(v.history()[0]["sha"])
    assert ok
    assert "revert me" in (vault / "Tasks/Inbox.md").read_text()
