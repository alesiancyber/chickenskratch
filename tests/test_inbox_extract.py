"""Free text typed straight into the task inbox becomes a proposal that
rewrites it into stamped tasks in place (inbox_extract); other people's
commitments route to the waiting list; intra-pass dedup means schema_fix
never fights it for the same note."""

import asyncio
import datetime as _dt

import pytest

from harness import proposals

TODAY = _dt.date.today().isoformat()

EXTRACTED = [
    {"text": "Buy a car", "due": None, "priority": None, "owed_by": None},
    {"text": "Test the detections", "due": None, "priority": None, "owed_by": None},
    {"text": "Investigate Azure docs", "due": "2026-07-28", "priority": None, "owed_by": None},
    {"text": "Follow up about the Northwind requests", "due": None, "priority": None,
     "owed_by": "Alex"},
]


async def fake_extractor(text, today):
    assert "buy a car" in text  # the raw prose reached the worker
    return list(EXTRACTED)


async def fake_schema_drafter(item):
    return {"title": f"Normalize {item['note']}", "summary": "stamp",
            "apply": [{"tool": "update_frontmatter",
                       "args": {"path": item["note"], "key": "id", "value": item["new_id"]}}]}


@pytest.fixture()
def vault(gitvault):
    # this suite tests the checkbox path — revoke auto-apply trust
    (gitvault / "Harness").mkdir(exist_ok=True)
    (gitvault / "Harness/Spec.md").write_text("---\nauto_apply: []\n---\n\n# Vault Spec\n")
    return gitvault


@pytest.fixture()
def watcher(vault, make_watcher):
    return make_watcher(vault, drafter=fake_schema_drafter,
                        capture_extractor=fake_extractor, n_max=20)


def run(w):
    return asyncio.run(w.run_once())


def inbox_rows(w):
    return [p for p in proposals.pending(w.proposals_dir) if p["kind"] == "inbox_extract"]


PROSE = """buy a car
test the detections
investigate azure docs by next tuesday
follow up with alex about northwind requests
"""


def test_inbox_free_text_round_trip(vault, watcher):
    inbox = vault / "Tasks/Inbox.md"
    before_tasks = [ln for ln in inbox.read_text().splitlines() if ln.startswith("- [")]
    inbox.write_text(inbox.read_text().rstrip("\n") + "\n" + PROSE)
    run(watcher)

    mine = [p for p in proposals.pending(watcher.proposals_dir)
            if p["drafted_against"]["note"] == "Tasks/Inbox.md"]
    assert len(mine) == 1, "intra-pass dedup: one proposal per note, priority wins"
    p = mine[0]
    assert p["kind"] == "inbox_extract"
    assert p["issues"][-1] == "waiting on Alex: Follow up about the Northwind requests"

    review = vault / "Harness/Review.md"
    review.write_text(review.read_text().replace(
        f"- [ ] approve {p['id']}", f"- [x] approve {p['id']}"))
    summary = run(watcher)
    assert summary["approved"] == 1

    after = inbox.read_text()
    for ln in before_tasks:
        assert ln in after.splitlines()  # pre-existing tasks byte-identical
    assert "buy a car" not in after  # prose rewritten, not duplicated
    assert f"- [ ] Buy a car ➕ {TODAY} ^t-" in after
    assert f"- [ ] Investigate Azure docs 📅 2026-07-28 ➕ {TODAY} ^t-" in after
    assert "Follow up about the Northwind" not in after  # owed item not in inbox
    assert "Alex: Follow up about the Northwind requests" in (
        vault / "Tasks/Waiting.md").read_text()


def test_rejected_inbox_text_not_redrafted_until_edited(vault, watcher):
    async def one_task(text, today):
        return [{"text": text.splitlines()[0], "due": None, "priority": None, "owed_by": None}]

    watcher.capture_extractor = one_task
    inbox = vault / "Tasks/Inbox.md"
    inbox.write_text(inbox.read_text().rstrip("\n") + "\nponder the void\n")
    run(watcher)
    (p,) = inbox_rows(watcher)
    review = vault / "Harness/Review.md"
    review.write_text(review.read_text().replace(
        f"- [ ] reject {p['id']}", f"- [x] reject {p['id']}"))
    run(watcher)
    run(watcher)
    assert inbox_rows(watcher) == []  # memo: same text never re-drafted
    inbox.write_text(inbox.read_text().rstrip("\n") + "\nand stare back\n")
    run(watcher)
    assert len(inbox_rows(watcher)) == 1  # editing re-arms
