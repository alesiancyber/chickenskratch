"""//commands: the vault is the command line. A typed //azure line is an
explicit order — executed on the next pass, replaced in place by ranked
links, no checkbox needed (user_invoked bypasses the auto_apply trust
list; the verifier and staleness checks still gate, and every write is
snapshot-bracketed)."""

import asyncio
import re

import pytest

from harness import processors, proposals
from harness.processors import INLINE_COMMAND

LINKS = ("- Alert correlation in Microsoft Sentinel — "
         "https://learn.microsoft.com/en-us/azure/sentinel/correlation\n"
         "  covers: correlating alerts into incidents")


@pytest.fixture()
def vault(gitvault):
    # trust list deliberately empty: a typed command must not need it
    (gitvault / "Harness").mkdir(exist_ok=True)
    (gitvault / "Harness/Spec.md").write_text("---\nauto_apply: []\n---\n\n# Vault Spec\n")
    return gitvault


@pytest.fixture()
def fake_azure(monkeypatch):
    calls = []

    async def fake(args, vault_path):
        calls.append(args)
        return LINKS

    monkeypatch.setitem(processors.INLINE_COMMANDS, "azure", fake)
    return calls


def run(w):
    return asyncio.run(w.run_once())


def test_typed_command_replaced_in_place_without_checkbox(vault, make_watcher, fake_azure):
    note = vault / "Tasks/detections.md"
    note.write_text("# Detections\n\nabove\n\n//azure alert correlation\n\nbelow\n")
    w = make_watcher(vault, registry=(INLINE_COMMAND,))
    summary = run(w)

    assert summary["auto"] == 1 and summary["drafted"] == 1
    assert fake_azure == ["alert correlation"]
    text = note.read_text()
    assert "**//azure alert correlation**" in text  # bolded echo keeps provenance
    assert "learn.microsoft.com" in text
    assert text.index("above") < text.index("learn.microsoft.com") < text.index("below")
    assert not re.search(r"^//azure", text, re.M)  # no re-trigger anchor left

    # applied without a checkbox, resolved as auto, and snapshot-committed
    assert "approve p-" not in (vault / "Harness/Review.md").read_text()
    assert [p["status"] for p in proposals.load_all(w.proposals_dir)] == ["auto"]
    from obsidian_mcp.vault import Vault
    assert any("replace line" in c["message"] for c in Vault(vault).history())


def test_prose_and_unknown_slashes_never_match(vault, make_watcher, fake_azure):
    note = vault / "Tasks/notes.md"
    body = "# Notes\n\n//todo tidy this later\n// a stray comment\n//azure\n"
    note.write_text(body)  # unknown command, bare comment, and //azure with no args
    w = make_watcher(vault, registry=(INLINE_COMMAND,))
    summary = run(w)
    assert summary["drafted"] == 0 and fake_azure == []
    assert note.read_text() == body  # untouched


def test_transient_failure_leaves_the_line_for_retry(vault, make_watcher, monkeypatch):
    async def down(args, vault_path):
        return None

    monkeypatch.setitem(processors.INLINE_COMMANDS, "azure", down)
    note = vault / "Tasks/detections.md"
    note.write_text("//azure alert correlation\n")
    w = make_watcher(vault, registry=(INLINE_COMMAND,))
    summary = run(w)
    assert summary["drafted"] == 0 and summary["auto"] == 0
    assert note.read_text().startswith("//azure alert correlation")  # stays armed


def test_enter_is_the_submit_key(vault, make_watcher, fake_azure):
    """A final line with no newline after it is still being typed: the
    command fires only once Enter terminates the line."""
    note = vault / "Tasks/typing.md"
    note.write_text("# T\n\n//azure alert correlation")  # no trailing newline
    w = make_watcher(vault, registry=(INLINE_COMMAND,))
    assert run(w)["drafted"] == 0 and fake_azure == []

    note.write_text("# T\n\n//azure alert correlation\n")  # Enter pressed
    assert run(w)["auto"] == 1
    assert "learn.microsoft.com" in note.read_text()


def test_command_typed_into_capture_surface_never_becomes_a_task():
    """Wrong-place safety: Capture.md is excluded from the command scan,
    and a //command typed there by habit must not be extracted as a task."""
    text = processors.capture_text(
        "# Capture\n\n//azure alert correlation\ncall the vendor tomorrow\n")
    assert text == "call the vendor tomorrow"
    assert processors.is_command_line("//azure alert correlation")
    assert processors.is_command_line("//azure")  # even bare: never task text
    assert not processors.is_command_line("//todo unknown commands are prose")


def test_dead_worker_never_starves_model_free_commands(vault, make_watcher):
    """Capture drafts through an LLM worker; //commands do not. A down
    worker fails capture's draft — the command behind it must still run."""
    from obsidian_mcp.vault import VaultError

    async def worker_down(text, today):
        raise VaultError("extract_tasks worker unavailable")

    async def links(args, vault_path):
        return LINKS

    (vault / "Harness/Capture.md").write_text(
        "# Capture\n\nremember to rotate the api keys\n")
    note = vault / "Tasks/detections.md"
    note.write_text("//azure alert correlation\n")
    w = make_watcher(vault, registry=(processors.CAPTURE, INLINE_COMMAND),
                     capture_extractor=worker_down)
    import unittest.mock as mock
    with mock.patch.dict(processors.INLINE_COMMANDS, {"azure": links}):
        summary = run(w)
    assert summary["auto"] == 1  # the command ran despite capture's dead worker
    assert "learn.microsoft.com" in note.read_text()


def test_command_outranks_pending_maintenance_on_the_same_note(vault, make_watcher, fake_azure):
    """The live failure of 2026-07-23: a grouping proposal pending against
    Tasks/Inbox.md silently swallowed a typed //azure there — the per-note
    queue invariant must never block a user-invoked command. The command
    applies; the maintenance proposal goes stale on the next pass."""
    note = vault / "Tasks/detections.md"
    note.write_text("# D\n\nsome text\n")
    w = make_watcher(vault, registry=(INLINE_COMMAND,))
    run(w)
    # plant a pending proposal against the same note
    p = proposals.new_proposal(
        kind="grouping", note="Tasks/detections.md", vault_sha="",
        note_content=note.read_text(), title="Group things", summary="s", issues=[],
        apply=[{"tool": "replace_section", "args": {
            "path": "Tasks/detections.md", "heading": "D", "content": "x"}}])
    proposals.save(p, w.proposals_dir)

    note.write_text("# D\n\nsome text\n\n//azure alert correlation\n")
    summary = run(w)
    assert summary["auto"] == 1  # the command was NOT queue-blocked
    assert "learn.microsoft.com" in note.read_text()
    # the maintenance proposal went stale (its note moved), never applied
    statuses = {q["id"]: q["status"] for q in proposals.load_all(w.proposals_dir)}
    assert statuses[p["id"]] == "stale"


def test_command_results_in_the_inbox_are_not_task_intent():
    """After //azure runs in the capture inbox, its echo, link lines, and
    indented continuations must not be re-extracted as tasks; real free
    text still is."""
    section = [
        "- [ ] existing task ^t-aaaaaaaaaaaa",
        "**//azure alert correlation**",
        "- Alert correlation — https://learn.microsoft.com/x",
        "  covers: correlating alerts into incidents",
        "call the vendor tomorrow",
    ]
    found = processors._inbox_prose("## Tasks\n" + "\n".join(section) + "\n")
    heading, _lines, _idx, prose = found
    assert heading == "Tasks"
    assert prose == ["call the vendor tomorrow"]


def test_verifier_rejects_plans_that_stray(vault, make_watcher):
    item = {"note": "Tasks/x.md", "line": "//azure q", "command": "azure", "args": "q"}
    good = {"apply": [{"tool": "replace_line", "args": {
        "path": "Tasks/x.md", "line_text": "//azure q", "content": "r"}}]}
    assert processors.verify_command(None, item, good) == []
    wrong_note = {"apply": [{"tool": "replace_line", "args": {
        "path": "Tasks/other.md", "line_text": "//azure q", "content": "r"}}]}
    assert processors.verify_command(None, item, wrong_note)
    wrong_tool = {"apply": [{"tool": "replace_section", "args": {}}]}
    assert processors.verify_command(None, item, wrong_tool)
