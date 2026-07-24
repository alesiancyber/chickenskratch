"""//commands: the vault is the command line, and commands are their own
class of change (2026-07-23 redesign) — user ORDERS, not harness
suggestions. Trust is intrinsic (typing is the approval), nothing ever
queues in Review.md, no proposal artifacts exist, staleness is the
replace_line anchor itself, and the n_max drafting budget is never
spent. The write substrate (gated tools, snapshot per mutation,
revertibility) is fully shared with proposals."""

import asyncio
import re

import pytest

from harness import commands, processors, proposals
from obsidian_mcp.vault import Vault

LINKS = ("- Alert correlation in Microsoft Sentinel — "
         "https://learn.microsoft.com/en-us/azure/sentinel/correlation\n"
         "  covers: correlating alerts into incidents")


@pytest.fixture()
def vault(gitvault):
    # empty trust list on purpose: commands must not need auto_apply
    (gitvault / "Harness").mkdir(exist_ok=True)
    (gitvault / "Harness/Spec.md").write_text("---\nauto_apply: []\n---\n\n# Vault Spec\n")
    return gitvault


@pytest.fixture()
def fake_azure(monkeypatch):
    calls = []

    async def fake(args, vault_path):
        calls.append(args)
        return LINKS

    monkeypatch.setitem(commands.INLINE_COMMANDS, "azure", fake)
    return calls


def run(w):
    return asyncio.run(w.run_once())


def test_typed_command_replaced_in_place_no_queue_no_artifacts(vault, make_watcher, fake_azure):
    note = vault / "Tasks/detections.md"
    note.write_text("# Detections\n\nabove\n\n//azure alert correlation\n\nbelow\n")
    w = make_watcher(vault, registry=())
    summary = run(w)

    assert summary["commands"] == 1 and summary["drafted"] == 0
    assert fake_azure == ["alert correlation"]
    text = note.read_text()
    assert "> [!NOTE] //azure alert correlation" in text  # callout, echo as title
    assert "> - Alert correlation" in text
    assert text.index("above") < text.index("learn.microsoft.com") < text.index("below")
    assert not re.search(r"^//azure", text, re.M)  # no re-trigger anchor left

    # a command leaves NO proposal artifact and NO checkbox — its audit
    # trail is the git snapshot
    assert proposals.load_all(w.proposals_dir) == []
    assert "approve p-" not in (vault / "Harness/Review.md").read_text()
    assert any("replace line" in c["message"] for c in Vault(vault).history())


def test_prose_and_unknown_slashes_never_match(vault, make_watcher, fake_azure):
    note = vault / "Tasks/notes.md"
    body = "# Notes\n\n//todo tidy this later\n// a stray comment\n//azure\n"
    note.write_text(body)  # unknown command, bare comment, and //azure with no args
    assert run(make_watcher(vault, registry=()))["commands"] == 0
    assert fake_azure == [] and note.read_text() == body


def test_enter_is_the_submit_key(vault, make_watcher, fake_azure):
    """A final line with no newline after it is still being typed: the
    command fires only once Enter terminates the line."""
    note = vault / "Tasks/typing.md"
    note.write_text("# T\n\n//azure alert correlation")  # no trailing newline
    w = make_watcher(vault, registry=())
    assert run(w)["commands"] == 0 and fake_azure == []

    note.write_text("# T\n\n//azure alert correlation\n")  # Enter pressed
    assert run(w)["commands"] == 1
    assert "learn.microsoft.com" in note.read_text()


def test_transient_failure_leaves_the_line_armed(vault, make_watcher, monkeypatch):
    async def down(args, vault_path):
        return None

    monkeypatch.setitem(commands.INLINE_COMMANDS, "azure", down)
    note = vault / "Tasks/detections.md"
    note.write_text("//azure alert correlation\n")
    summary = run(make_watcher(vault, registry=()))
    assert summary["commands"] == 0
    assert note.read_text().startswith("//azure alert correlation")  # stays armed


def test_command_outranks_pending_maintenance_on_the_same_note(vault, make_watcher, fake_azure):
    """Commands never consult the proposal queue: a pending proposal on
    the same note neither blocks the command nor survives it (the apply
    moves the note, so the proposal auto-stales next pass)."""
    note = vault / "Tasks/detections.md"
    note.write_text("# D\n\nsome text\n")
    w = make_watcher(vault, registry=())
    run(w)
    p = proposals.new_proposal(
        kind="grouping", note="Tasks/detections.md", vault_sha="",
        note_content=note.read_text(), title="Group things", summary="s", issues=[],
        apply=[{"tool": "replace_section", "args": {
            "path": "Tasks/detections.md", "heading": "D", "content": "x"}}])
    proposals.save(p, w.proposals_dir)

    note.write_text("# D\n\nsome text\n\n//azure alert correlation\n")
    assert run(w)["commands"] == 1
    assert "learn.microsoft.com" in note.read_text()
    statuses = {q["id"]: q["status"] for q in proposals.load_all(w.proposals_dir)}
    assert statuses[p["id"]] == "stale"


def test_command_applies_even_when_note_edited_during_execution(vault, make_watcher, monkeypatch):
    """Staleness is the line anchor, nothing more: the user typing
    elsewhere in the note while the search runs never gates the apply."""
    note = vault / "Tasks/inbox-like.md"
    note.write_text("# Inbox\n\n- [ ] a task ^t-aaaaaaaaaaaa\n\n//azure alert correlation\n")

    async def user_kept_typing(args, vault_path):
        note.write_text(note.read_text().replace("a task", "a task, now reworded"))
        return LINKS

    monkeypatch.setitem(commands.INLINE_COMMANDS, "azure", user_kept_typing)
    summary = run(make_watcher(vault, registry=()))
    assert summary["commands"] == 1
    text = note.read_text()
    assert "now reworded" in text and "learn.microsoft.com" in text


def test_command_line_deleted_during_execution_lapses_silently(vault, make_watcher, monkeypatch):
    note = vault / "Tasks/inbox-like.md"
    note.write_text("//azure alert correlation\n")

    async def user_deleted_it(args, vault_path):
        note.write_text("# changed my mind\n")
        return LINKS

    monkeypatch.setitem(commands.INLINE_COMMANDS, "azure", user_deleted_it)
    w = make_watcher(vault, registry=())
    summary = run(w)
    assert summary["commands"] == 0
    assert note.read_text() == "# changed my mind\n"  # the user's edit wins
    assert proposals.load_all(w.proposals_dir) == []  # and no artifact remains


def test_dead_worker_never_starves_commands(vault, make_watcher, monkeypatch):
    """Capture drafts through an LLM worker; commands run in their own
    phase before drafting — a down worker cannot touch them."""
    from obsidian_mcp.vault import VaultError

    async def worker_down(text, today):
        raise VaultError("extract_tasks worker unavailable")

    async def links(args, vault_path):
        return LINKS

    monkeypatch.setitem(commands.INLINE_COMMANDS, "azure", links)
    (vault / "Harness/Capture.md").write_text(
        "# Capture\n\nremember to rotate the api keys\n")
    note = vault / "Tasks/detections.md"
    note.write_text("//azure alert correlation\n")
    w = make_watcher(vault, registry=(processors.CAPTURE,), capture_extractor=worker_down)
    summary = run(w)
    assert summary["commands"] == 1
    assert "learn.microsoft.com" in note.read_text()


def test_commands_kill_switch_rides_disabled_processors(vault, make_watcher, fake_azure):
    """`disabled_processors: [inline_command]` still turns commands off —
    the name is peeled off before the registry (which would reject it)."""
    note = vault / "Tasks/detections.md"
    note.write_text("//azure alert correlation\n")
    w = make_watcher(vault, disabled=("inline_command",))
    assert run(w)["commands"] == 0
    assert fake_azure == []
    assert note.read_text().startswith("//azure")


def test_command_typed_into_capture_surface_never_becomes_a_task():
    """Wrong-place safety: Capture.md is excluded from the command scan,
    and a //command typed there by habit must not be extracted as a task."""
    text = processors.capture_text(
        "# Capture\n\n//azure alert correlation\ncall the vendor tomorrow\n")
    assert text == "call the vendor tomorrow"
    assert commands.is_command_line("//azure alert correlation")
    assert commands.is_command_line("//azure")  # even bare: never task text
    assert not commands.is_command_line("//todo unknown commands are prose")


def test_command_results_in_the_inbox_are_not_task_intent():
    """A command result (callout title, quoted links, indented
    continuations) must not be re-extracted as tasks; real free text
    still is."""
    section = [
        "- [ ] existing task ^t-aaaaaaaaaaaa",
        "> [!NOTE] //azure alert correlation",
        "> - Alert correlation — https://learn.microsoft.com/x",
        ">   covers: correlating alerts into incidents",
        "call the vendor tomorrow",
    ]
    found = processors._inbox_prose("## Tasks\n" + "\n".join(section) + "\n")
    heading, _lines, _idx, prose = found
    assert heading == "Tasks"
    assert prose == ["call the vendor tomorrow"]


def test_callout_type_knob_is_a_spec_edit(vault, make_watcher, fake_azure):
    (vault / "Harness/Spec.md").write_text(
        "---\nauto_apply: []\ncallout_type: TIP\n---\n\n# Vault Spec\n")
    note = vault / "Tasks/detections.md"
    note.write_text("//azure alert correlation\n")
    assert run(make_watcher(vault, registry=()))["commands"] == 1
    assert "> [!TIP] //azure alert correlation" in note.read_text()
