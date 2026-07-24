"""Phase-0 foundations: jsonval, the vault spec's folder→type
map, id/block-ref primitives, Private/ exclusions, Chat-Id trailers."""

import pytest

import obsidian_mcp.vault as vault_mod
from obsidian_mcp import ids, jsonval, server, tasks, vaultconfig

# ---- jsonval (B1) ----

def test_validate_returns_readable_paths():
    schema = {"type": "object", "required": ["id"],
              "properties": {"attendees": {"type": "array"}}}
    issues = jsonval.validate({"attendees": 3}, schema)
    assert any(i.startswith("(root): 'id' is a required") for i in issues)
    assert any(i.startswith("attendees:") for i in issues)
    assert jsonval.validate({"id": "x", "attendees": []}, schema) == []


# ---- vault config / typing (B2) ----

def test_folder_type_map_is_the_typing_source():
    assert server._note_type({}, "Tasks/Inbox.md") == "task"
    assert server._note_type({}, "Projects/X.md") == "project"    # boards live here too
    assert server._note_type({}, "Stuff/X.md") == "stuff"         # unmapped folder
    assert server._note_type({}, "Home.md") == "other"            # root note
    # folders are broad categories: the note's own frontmatter carries the
    # specific type (2026-07-22 folder cleanup)
    assert server._note_type({"type": "deliverable"}, "Tasks/Owed.md") == "deliverable"
    assert server._note_type({"type": "Research"}, "Tasks/x.md") == "research"  # fm wins
    assert server._note_type({"kanban-plugin": "board"}, "Projects/x.md") == "kanban"


def test_missing_config_file_defaults(tmp_path):
    cfg = vaultconfig.load_config(tmp_path / "nope.yaml")
    assert cfg.folder_types == {}
    assert cfg.exclude == ("Private",)   # secure by default


# ---- ids / block refs (B3) ----

def test_stamp_id_format():
    a, b = ids.stamp_id("t"), ids.stamp_id("t")
    assert a != b and a.startswith("t-") and len(a) == 14
    assert len(ids.stamp_id()) == 12


def test_block_ref_round_trip():
    line, ref = ids.split_block_ref("- [ ] Ship it ^t-abc123")
    assert (line, ref) == ("- [ ] Ship it", "t-abc123")
    assert ids.attach_block_ref(line, ref) == "- [ ] Ship it ^t-abc123"
    assert ids.split_block_ref("- [ ] math is 2^10") == ("- [ ] math is 2^10", None)


def test_parse_task_line_strips_block_ref():
    t = tasks.parse_task_line("- [ ] Ship it ⏫ 📅 2026-07-25 ^t-abc123")
    assert t.text == "Ship it" and t.block_id == "t-abc123"
    assert t.due == "2026-07-25" and t.priority == "high"
    assert t.to_dict("Tasks/Inbox.md")["id"] == "t-abc123"
    plain = tasks.parse_task_line("- [ ] Ship it")
    assert plain.block_id is None and "id" not in plain.to_dict("x.md")


def test_format_task_with_block_id():
    line = tasks.format_task("Ship it", due="2026-07-25", priority="high", block_id="t-abc123")
    assert line == "- [ ] Ship it ⏫ 📅 2026-07-25 ^t-abc123"
    t = tasks.parse_task_line(line)
    assert t.text == "Ship it" and t.block_id == "t-abc123"


def test_set_task_status_keeps_block_ref_last(vault):
    inbox = vault / "Tasks/Inbox.md"
    inbox.write_text(inbox.read_text() + "- [ ] Stamped thing ^t-abc123\n")
    server.set_task_status("Tasks/Inbox", "Stamped thing", done=True)
    line = next(ln for ln in inbox.read_text().splitlines() if "Stamped thing" in ln)
    assert line.startswith("- [x] Stamped thing ✅ ")
    assert line.endswith(" ^t-abc123")


# ---- Private/ exclusions (B5) ----

def test_private_invisible_to_all_reads(vault):
    assert not any("Private" in n for n in server.list_notes()["notes"])
    assert server.search_vault("PRIVATE-FIXTURE-MARKER")["total"] == 0
    texts = [t["text"] for t in server.list_tasks(status="all", limit=500)["tasks"]]
    assert not any("PRIVATE-FIXTURE-TASK" in t for t in texts)


def test_private_read_and_write_rejected(vault):
    with pytest.raises(vault_mod.VaultError, match="excluded"):
        server.read_note("Private/Client Notes")
    with pytest.raises(vault_mod.VaultError, match="excluded"):
        server.append_to_note("Private/Client Notes", "leak")
    # a fuzzy miss never suggests an excluded note
    with pytest.raises(vault_mod.VaultError) as e:
        server.read_note("Client Notes")
    assert "Private" not in str(e.value)


def test_private_case_variants_rejected(vault):
    # macOS/Windows filesystems are case-insensitive: private/ IS Private/,
    # and small models lowercase paths constantly.
    for variant in ("private/Client Notes", "PRIVATE/Client Notes.md", "pRiVaTe/Client Notes"):
        with pytest.raises(vault_mod.VaultError, match="excluded"):
            server.read_note(variant)
    with pytest.raises(vault_mod.VaultError, match="excluded"):
        server.append_to_note("private/leak", "leak")


def test_symlinked_notes_do_not_leak(vault):
    outside = vault.parent / "outside.md"
    outside.write_text("OUTSIDE-SECRET\n")
    (vault / "Outside Link.md").symlink_to(outside)
    (vault / "Private Link.md").symlink_to(vault / "Private" / "Client Notes.md")
    assert not any("Link" in n for n in server.list_notes()["notes"])
    assert server.search_vault("PRIVATE-FIXTURE-MARKER")["total"] == 0
    assert server.search_vault("OUTSIDE-SECRET")["total"] == 0


def test_write_is_atomic_and_leaves_no_litter(vault):
    import obsidian_mcp.vault as vm
    v = vm.get_vault()
    v.write("Tasks/Atomic.md", "one\n")
    v.write("Tasks/Atomic.md", "two\n")
    assert (vault / "Tasks/Atomic.md").read_text() == "two\n"
    assert not list((vault / "Tasks").glob("*.tmp"))


# ---- Chat-Id trailer (B4, vault side) ----

def test_snapshot_after_embeds_chat_id_trailer(vault, monkeypatch):
    monkeypatch.setenv("HARNESS_CHAT_ID", "20260722-test-chat")
    monkeypatch.setattr(vault_mod, "_vault", None)
    server.add_task("Tasks/Inbox", "attributed task")
    v = vault_mod.get_vault()
    top = v.history()[0]
    assert "Chat-Id" not in top["message"]           # subject stays clean
    assert "Chat-Id: 20260722-test-chat" in v.commit_message(top["sha"])


def test_snapshot_before_never_carries_chat_id(vault, monkeypatch):
    monkeypatch.setenv("HARNESS_CHAT_ID", "20260722-test-chat")
    monkeypatch.setattr(vault_mod, "_vault", None)
    server.add_task("Tasks/Inbox", "init repo")
    (vault / "Manual.md").write_text("a manual edit\n")
    server.add_task("Tasks/Inbox", "second")
    v = vault_mod.get_vault()
    manual = next(c for c in v.history() if c["message"].startswith("manual edits"))
    assert "Chat-Id" not in v.commit_message(manual["sha"])


def test_no_chat_id_env_no_trailer(vault):
    server.add_task("Tasks/Inbox", "plain task")
    v = vault_mod.get_vault()
    assert "Chat-Id" not in v.commit_message(v.history()[0]["sha"])


# ---- Chat-Id env (B4, harness side) ----

def test_stdio_env_carries_chat_id():
    from harness.mcp_client import MultiServerClient, ServerSpec
    spec = ServerSpec(name="obsidian", transport="stdio", command=["x"], env={"A": "1"})
    with_id = MultiServerClient([spec], chat_id="chat-42")._stdio_env(spec)
    assert with_id["HARNESS_CHAT_ID"] == "chat-42" and with_id["A"] == "1"
    without = MultiServerClient([spec])._stdio_env(spec)
    assert "HARNESS_CHAT_ID" not in without


def test_editor_knobs_override_and_validate():
    from pathlib import Path

    base = vaultconfig.VaultConfig(
        folder_types={}, exclude=(), surfaces={}, schemas_dir=Path("."))
    out = vaultconfig._spec_overrides(
        base, {"callout_type": "TIP", "link_scheme": "vscode", "daily_dir": "journal/"})
    assert out.callout_type == "TIP"
    assert out.link_scheme == "vscode"
    assert out.daily_dir == "journal"          # trailing slash normalized
    bad = vaultconfig._spec_overrides(base, {"link_scheme": "emacs", "callout_type": "  "})
    assert bad.link_scheme == "obsidian"       # unknown scheme ignored
    assert bad.callout_type == "NOTE"          # blank ignored, default holds
