"""Harness/Changes.md (Phase 10): the audit surface for a system that
now edits without asking. Renders the vault's git log, filters its own
render commits, and never feeds itself."""

import asyncio

import pytest

from obsidian_mcp.vault import Vault


@pytest.fixture()
def vault(gitvault):
    return gitvault


def run(w):
    return asyncio.run(w.run_once())


def test_changes_surface_renders_and_never_loops(vault, make_watcher):
    w = make_watcher(vault, registry=())
    run(w)
    surface = vault / "Harness/Changes.md"
    text = surface.read_text()
    assert "seed Harness/Spec.md" in text  # this pass's own commits are news
    assert "update Harness/Changes.md" not in text  # ...except its own renders
    stamp = surface.stat().st_mtime

    run(w)  # nothing new happened: identical render, no write, no loop
    assert surface.read_text() == text
    assert surface.stat().st_mtime == stamp


def test_new_commits_appear_with_files_and_chat_attribution(vault, make_watcher):
    w = make_watcher(vault, registry=())
    run(w)
    v = Vault(vault)
    (vault / "Tasks/Inbox.md").write_text(
        (vault / "Tasks/Inbox.md").read_text() + "- [ ] hand edit\n")
    v.commit_if_dirty("manual edits (test)")
    v._git("commit", "--allow-empty", "-m", "chat edit", "-m", "Chat-Id: chat-abc123def")
    run(w)
    text = (vault / "Harness/Changes.md").read_text()
    assert "manual edits (test)" in text
    assert "`Tasks/Inbox.md`" in text  # files touched are listed
    assert "chat edit" in text
    assert "· chat `chat-abc123d" in text  # Chat-Id trailer surfaced (12-char prefix)
