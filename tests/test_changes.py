"""The audit view for a system that edits without asking: renders the
vault's git log and filters out the harness redrawing its own surfaces.
Stopped being a note in the vault on 2026-07-26 — it is now what
`harness changes` prints and what /api/changes serves."""

import asyncio

import pytest

from obsidian_mcp.vault import Vault


@pytest.fixture()
def vault(gitvault):
    return gitvault


def run(w):
    return asyncio.run(w.run_once())


def view(vault, limit=20):
    from harness import changes
    from obsidian_mcp.vaultconfig import get_config

    return changes.render(Vault(vault), get_config(str(vault)), limit)


def test_changes_renders_news_and_filters_harness_redraws(vault, make_watcher):
    w = make_watcher(vault, registry=())
    run(w)
    text = view(vault)
    assert "seed Harness/Spec.md" in text        # this pass's own commits are news
    # the retired surfaces' historical render commits stay filtered, so a
    # vault upgraded from the surface era doesn't drown in old redraws
    assert "update Harness/Changes.md" not in text
    assert "update Harness/Review.md" not in text
    assert "update Harness/Today.md" not in text

    run(w)  # nothing new happened: identical render, and no note written
    assert view(vault) == text
    assert not (vault / "Harness/Changes.md").exists()


def test_new_commits_appear_with_files_and_chat_attribution(vault, make_watcher):
    w = make_watcher(vault, registry=())
    run(w)
    v = Vault(vault)
    (vault / "Tasks/Inbox.md").write_text(
        (vault / "Tasks/Inbox.md").read_text() + "- [ ] hand edit\n")
    v.commit_if_dirty("manual edits (test)")
    v._git("commit", "--allow-empty", "-m", "chat edit", "-m", "Chat-Id: chat-abc123def")
    run(w)
    text = view(vault)
    assert "manual edits (test)" in text
    assert "`Tasks/Inbox.md`" in text  # files touched are listed
    assert "chat edit" in text
    assert "· chat `chat-abc123d" in text  # Chat-Id trailer surfaced (12-char prefix)
