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
