"""The vault-side capture surface:
Harness/Capture.md → extraction worker → capture proposal in Review.md →
approve adds tasks to the inbox and clears the surface. A scripted fake
extractor stands in for the worker LLM."""

import asyncio

import pytest

import obsidian_mcp.vault as vault_mod
from harness import proposals
from harness.processors import CAPTURE_SKELETON
from obsidian_mcp.vault import Vault

EXTRACTED = [
    {"text": "Migrate the DNS records", "due": "2026-07-24", "priority": "high"},
    {"text": "Revisit the pricing page", "due": None, "priority": None},
]


async def fake_extractor(text, today):
    return list(EXTRACTED)


async def fake_schema_drafter(item):
    return {
        "title": f"Normalize {item['note']}",
        "summary": "stamp",
        "apply": [{"tool": "update_frontmatter",
                   "args": {"path": item["note"], "key": "id", "value": item["new_id"]}}],
    }


@pytest.fixture()
def vault(gitvault):
    # this suite tests the checkbox path — revoke auto-apply trust
    (gitvault / "Harness").mkdir(exist_ok=True)
    (gitvault / "Harness/Spec.md").write_text("---\nauto_apply: []\n---\n\n# Vault Spec\n")
    return gitvault


@pytest.fixture()
def watcher(vault, make_watcher):
    return make_watcher(vault, drafter=fake_schema_drafter,
                        capture_extractor=fake_extractor, n_max=10)


def run(w):
    return asyncio.run(w.run_once())


def capture_rows(w):
    return [p for p in proposals.pending(w.proposals_dir) if p["kind"] == "capture"]


def type_into_capture(vault, text):
    p = vault / "Harness/Capture.md"
    p.write_text(p.read_text() + text)


def test_first_pass_creates_the_surface(vault, watcher):
    run(watcher)
    surface = vault / "Harness/Capture.md"
    assert surface.read_text() == CAPTURE_SKELETON
    assert any("Harness/Capture.md" in c["files"] for c in Vault(vault).history())
    assert capture_rows(watcher) == []  # skeleton alone is not capture input


def test_typed_text_becomes_a_capture_proposal(vault, watcher):
    run(watcher)
    type_into_capture(vault, "\ncall the vendor about DNS, urgent, and pricing page someday\n")
    run(watcher)
    rows = capture_rows(watcher)
    assert len(rows) == 1
    p = rows[0]
    assert p["drafted_against"]["note"] == "Harness/Capture.md"
    assert [s["tool"] for s in p["apply"]] == ["add_task", "add_task"]
    assert p["apply"][0]["args"] == {
        "path": "Tasks/Inbox.md", "text": "Migrate the DNS records",
        "due": "2026-07-24", "priority": "high",
    }
    review = (vault / "Harness/Review.md").read_text()
    assert "Migrate the DNS records — due 2026-07-24, high" in review
    assert f"- [ ] approve {p['id']}" in review
    # capture proposals list their tasks; no meaningless diff block
    section = review.split(p["title"])[1]
    assert "```diff" not in section.split("---")[0]


def test_same_text_is_never_drafted_twice(vault, watcher):
    run(watcher)
    type_into_capture(vault, "\nsome capture text\n")
    run(watcher)
    (p,) = capture_rows(watcher)
    # while pending: covered; after reject: hash memo
    run(watcher)
    assert len(capture_rows(watcher)) == 1
    review = vault / "Harness/Review.md"
    review.write_text(review.read_text().replace(
        f"- [ ] reject {p['id']}", f"- [x] reject {p['id']}"))
    run(watcher)
    run(watcher)
    assert capture_rows(watcher) == []  # rejected text not re-extracted
    # …until the user edits the surface again
    type_into_capture(vault, "\nnew thought\n")
    run(watcher)
    assert len(capture_rows(watcher)) == 1


def test_approve_adds_tasks_and_clears_the_surface(vault, watcher, monkeypatch):
    recorded = []
    monkeypatch.setattr("harness.watcher.record_dashboard",
                        lambda p, d, **k: recorded.append((p["kind"], d)))
    run(watcher)
    type_into_capture(vault, "\nmigrate dns and revisit pricing\n")
    run(watcher)
    (p,) = capture_rows(watcher)

    review = vault / "Harness/Review.md"
    review.write_text(review.read_text().replace(
        f"- [ ] approve {p['id']}", f"- [x] approve {p['id']}"))
    summary = run(watcher)

    assert summary["approved"] == 1
    inbox = (vault / "Tasks/Inbox.md").read_text()
    assert "Migrate the DNS records ⏫ 📅 2026-07-24" in inbox
    assert "Revisit the pricing page" in inbox
    assert (vault / "Harness/Capture.md").read_text() == CAPTURE_SKELETON
    assert ("capture", "approved") in recorded
    # added tasks are born stamped -> they conform, and the surface reset
    # is its own commit in history
    assert any("clear Harness/Capture.md" in c["subject"] for c in Vault(vault).history())


def test_owed_items_route_to_the_waiting_list(vault, watcher):
    async def mixed(text, today):
        return [
            {"text": "Review the vendor contract", "due": "2026-08-05", "priority": None, "owed_by": None},
            {"text": "Send the vendor contract", "due": None, "priority": None, "owed_by": "Nadia"},
        ]

    watcher.capture_extractor = mixed
    run(watcher)
    type_into_capture(vault, "\nnadia will send the contract; review it in two weeks\n")
    run(watcher)
    (p,) = capture_rows(watcher)
    assert p["title"] == "Add 1 captured task(s) to Tasks/Inbox.md + 1 waiting-on item(s) to Tasks/Waiting.md"
    assert p["apply"][0]["args"]["path"] == "Tasks/Inbox.md"
    assert p["apply"][1]["args"] == {"path": "Tasks/Waiting.md", "text": "Nadia: Send the vendor contract"}
    assert "waiting on Nadia: Send the vendor contract" in (vault / "Harness/Review.md").read_text()

    review = vault / "Harness/Review.md"
    review.write_text(review.read_text().replace(
        f"- [ ] approve {p['id']}", f"- [x] approve {p['id']}"))
    summary = run(watcher)
    assert summary["approved"] == 1
    # waiting list is created on first use, task born stamped
    assert "Nadia: Send the vendor contract" in (vault / "Tasks/Waiting.md").read_text()
    assert "Review the vendor contract" in (vault / "Tasks/Inbox.md").read_text()
    assert (vault / "Harness/Capture.md").read_text() == CAPTURE_SKELETON


def test_empty_extraction_marks_processed_without_a_proposal(vault, watcher, monkeypatch):
    async def nothing(text, today):
        return []

    watcher.capture_extractor = nothing
    run(watcher)
    type_into_capture(vault, "\njust musing, no tasks here\n")
    run(watcher)
    assert capture_rows(watcher) == []
    run(watcher)
    assert capture_rows(watcher) == []  # not re-extracted every pass


def test_worker_down_completes_the_pass(vault, watcher):
    """A dead capture worker fails capture's own drafts (retried next
    pass, no proposal enters the queue) but no longer halts the pass or
    the processors behind it — see the //commands starvation fix."""
    async def down(text, today):
        raise vault_mod.VaultError("worker unavailable")

    watcher.capture_extractor = down
    run(watcher)
    type_into_capture(vault, "\nsomething\n")
    summary = run(watcher)
    assert capture_rows(watcher) == []  # capture itself drafted nothing
    assert summary["scanned"]           # and the pass still completed
