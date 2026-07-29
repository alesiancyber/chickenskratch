"""Phase-0 foundations: jsonval, the vault spec's folder→type
map, id/block-ref primitives, Private/ exclusions, Chat-Id trailers."""

import pytest

import tezcat_vault.vault as vault_mod
from tezcat_vault import ids, jsonval, server, tasks, vaultconfig

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
    import datetime as dt
    today = dt.date.today().isoformat()
    # ➕ birth date stamps at the choke point: every harness-born task
    # carries lifecycle metadata globally (2026-07-23 principle)
    assert line == f"- [ ] Ship it ⏫ 📅 2026-07-25 ➕ {today} ^t-abc123"
    parsed = tasks.parse_task_line(line)
    assert parsed.created == today and parsed.text == "Ship it"
    assert tasks.format_task("Bare", created=None) == "- [ ] Bare"
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
    import tezcat_vault.vault as vm
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
    assert "Chat-Id" not in top["subject"]           # subject stays clean
    assert top["chat_id"] == "20260722-test-chat"    # parsed from the trailer
    assert "Chat-Id: 20260722-test-chat" in v.commit_message(top["sha"])


def test_snapshot_before_never_carries_chat_id(vault, monkeypatch):
    monkeypatch.setenv("HARNESS_CHAT_ID", "20260722-test-chat")
    monkeypatch.setattr(vault_mod, "_vault", None)
    server.add_task("Tasks/Inbox", "init repo")
    (vault / "Manual.md").write_text("a manual edit\n")
    server.add_task("Tasks/Inbox", "second")
    v = vault_mod.get_vault()
    manual = next(c for c in v.history() if c["subject"].startswith("manual edits"))
    assert "Chat-Id" not in v.commit_message(manual["sha"])


def test_no_chat_id_env_no_trailer(vault):
    server.add_task("Tasks/Inbox", "plain task")
    v = vault_mod.get_vault()
    assert "Chat-Id" not in v.commit_message(v.history()[0]["sha"])


def test_renderer_knobs_left_spec_with_the_owned_ui():
    """The 0-B move: the vault describes its content, the repo describes
    its renderers. A Spec.md key for a deleted renderer knob
    (diff_style/callout_type/link_scheme) is inert, never a crash."""
    from pathlib import Path

    base = vaultconfig.VaultConfig(
        folder_types={}, exclude=(), surfaces={}, schemas_dir=Path("."))
    out = vaultconfig._spec_overrides(
        base, {"callout_type": "TIP", "link_scheme": "vscode",
               "diff_style": "unified", "daily_dir": "journal/"})
    assert not hasattr(out, "callout_type") and not hasattr(out, "diff_style")
    assert not hasattr(out, "link_scheme")     # deleted 2026-07-26
    assert out.daily_dir == "journal"          # content knobs still live here


def test_a_stale_link_scheme_in_vault_yaml_is_inert(tmp_path):
    """The knob is gone (tezcat opens notes in tezcat). A config file that
    still carries it loads fine and simply ignores it — an old vault.yaml
    must never wedge the tezcat."""
    cfg = tmp_path / "vault.yaml"
    cfg.write_text("link_scheme: vscode\n")
    loaded = vaultconfig.load_config(cfg)
    assert not hasattr(loaded, "link_scheme")


def test_the_vault_lock_is_bounded_not_forever(tmp_path, monkeypatch):
    """A wedged holder must cost ONE operation a clean error. Unbounded,
    it deadlocked the engine: a pass holds the PASS lock while waiting for
    this one, so every later trigger then no-opped forever."""
    import fcntl
    import hashlib
    import tempfile
    import time
    from pathlib import Path

    import pytest

    from tezcat_vault.vault import Vault, VaultError

    root = tmp_path / "v"
    root.mkdir()
    v = Vault(root)
    digest = hashlib.sha1(str(v.root).encode()).hexdigest()[:12]
    lock_path = Path(tempfile.gettempdir()) / f"tezcat-vault-{digest}.lock"
    with open(lock_path, "w") as wedged:
        fcntl.flock(wedged, fcntl.LOCK_EX)
        start = time.monotonic()
        with pytest.raises(VaultError, match="held by another process"):
            with v.locked(timeout=0.3):
                pass
        assert time.monotonic() - start < 5  # bounded, not blocked


def test_meetings_dir_is_a_spec_key_like_daily_dir(vault, monkeypatch):
    """Both "where does this note type live" keys are config, not
    literals — renaming the folder must not silently empty the dashboard's
    meeting sections."""
    import datetime as dt

    import tezcat_vault.vault as vault_mod
    from tezcat import today as today_mod
    from tezcat_vault import server, vaultconfig

    spec = vault / "Harness" / "Spec.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("---\nmeetings_dir: Standups\n---\n")
    vaultconfig._merged_cache.clear()
    monkeypatch.setattr(vault_mod, "_vault", None)

    cfg = vaultconfig.get_config(str(vault))
    assert cfg.meetings_dir == "Standups"
    # the create tool follows it
    rel = server.create_meeting_note("Weekly", date="2026-07-20")["created"]
    assert rel.startswith("Standups/")
    # ...and so does the attention surface, through the same key
    model = today_mod.collect(vault_mod.get_vault(), cfg, 0, dt.date(2026, 7, 20))
    assert model["meetings"] == ["Standups/2026-07-20 Weekly.md"]
    vaultconfig._merged_cache.clear()


def test_policy_for_resolves_the_map_with_kill_switch_precedence(vault, monkeypatch):
    """The policies map is the truth; a processor it doesn't name takes
    DEFAULT_POLICIES; disabled_processors outranks everything — it is
    the emergency kill switch, and a map naming every processor must
    never render it inert. One resolver, so the engine and the console
    can never read the same Spec differently."""
    import tezcat_vault.vault as vault_mod

    spec = vault / "Harness" / "Spec.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(
        "---\n"
        "policies: {capture: full, grouping: verified, overlap: junklevel}\n"
        "disabled_processors: [schema_fix]\n"
        "---\n")
    vaultconfig._merged_cache.clear()
    monkeypatch.setattr(vault_mod, "_vault", None)

    cfg = vaultconfig.get_config(str(vault))
    assert cfg.policy_for("capture") == "full"        # explicit
    assert cfg.policy_for("grouping") == "verified"   # explicit, even verifier-less
    assert cfg.policy_for("schema_fix") == "off"      # kill switch outranks default
    assert cfg.policy_for("plan_today") == "ask"      # named nowhere, no default grant
    # a junk level is dropped at parse, so overlap falls to its default
    # (ask) instead of silently meaning something the user didn't write
    assert cfg.policy_for("overlap") == "ask"
    vaultconfig._merged_cache.clear()


def test_policy_defaults_grant_only_the_checkable_lanes(vault, monkeypatch):
    import tezcat_vault.vault as vault_mod

    spec = vault / "Harness" / "Spec.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("---\nn_max: 5\n---\n")
    vaultconfig._merged_cache.clear()
    monkeypatch.setattr(vault_mod, "_vault", None)

    cfg = vaultconfig.get_config(str(vault))
    assert cfg.policy_for("capture") == "verified"
    assert cfg.policy_for("schema_fix") == "verified"
    assert cfg.policy_for("grouping") == "ask"        # judgment asks by default
    assert cfg.policy_for("plan_today") == "ask"
    vaultconfig._merged_cache.clear()
