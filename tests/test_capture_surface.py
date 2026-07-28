"""The vault-side capture surface:
Harness/Capture.md → extraction worker → capture proposal in the queue →
approve adds tasks to the inbox and clears the surface. A scripted fake
extractor stands in for the worker LLM."""

import asyncio
import json

import pytest

import tezcat_vault.vault as vault_mod
from tezcat import proposals
from tezcat.processors import CAPTURE_SKELETON
from tezcat_vault.vault import Vault

EXTRACTED = [
    {"text": "Migrate the DNS records", "due": "2026-07-24", "priority": "high"},
    {"text": "Revisit the pricing page", "due": None, "priority": None},
]


# every processor but capture: these tests are about the extraction path
OTHERS = ("schema_fix", "grouping", "spec_drift", "archive_done", "overlap",
          "plan_today", "inbox_extract")


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
    # the proposal carries its tasks as issues — what the dashboard
    # renders as steps, and what partial approval picks from
    assert "Migrate the DNS records — due 2026-07-24, high" in p["issues"]
    # capture is a no-diff kind: its issues list IS the payload
    from tezcat.processors import REGISTRY
    assert not next(s for s in REGISTRY if s.name == "capture").render_diff


def test_same_text_is_never_drafted_twice(vault, watcher):
    run(watcher)
    type_into_capture(vault, "\nsome capture text\n")
    run(watcher)
    (p,) = capture_rows(watcher)
    # while pending: covered; after reject: hash memo
    run(watcher)
    assert len(capture_rows(watcher)) == 1
    asyncio.run(watcher.run_once(decisions=[(p['id'], "reject")]))
    run(watcher)
    assert capture_rows(watcher) == []  # rejected text not re-extracted
    # …until the user edits the surface again
    type_into_capture(vault, "\nnew thought\n")
    run(watcher)
    assert len(capture_rows(watcher)) == 1


def test_approve_adds_tasks_and_clears_the_surface(vault, watcher, monkeypatch):
    recorded = []
    monkeypatch.setattr("tezcat.watcher.record_dashboard",
                        lambda p, d, **k: recorded.append((p["kind"], d)))
    run(watcher)
    type_into_capture(vault, "\nmigrate dns and revisit pricing\n")
    run(watcher)
    (p,) = capture_rows(watcher)

    summary = asyncio.run(watcher.run_once(decisions=[(p['id'], "approve")]))

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
    assert "waiting on Nadia: Send the vendor contract" in p["issues"]

    summary = asyncio.run(watcher.run_once(decisions=[(p['id'], "approve")]))
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


def test_a_dead_worker_is_reported_not_reported_as_nothing_to_extract(
        vault, make_watcher):
    """The regression that told a user their dump was empty: extraction
    raised (rate-limited worker), the pass swallowed it, and every surface
    said "nothing extractable". A failed draft must be RECORDED — the text
    is still on the surface, still re-arms, and the user must be told to
    expect a retry rather than that they typed nothing."""
    from tezcat_vault.vault import VaultError

    async def dead_worker(text, today):
        raise VaultError("extract_tasks worker (some-model) unavailable: 429")

    (vault / "Harness/Capture.md").write_text(
        CAPTURE_SKELETON + "\nbuy milk and call the vet tomorrow\n")
    w = make_watcher(vault, capture_extractor=dead_worker, disabled=OTHERS)
    summary = asyncio.run(w.run_once())

    assert summary["drafted"] == 0          # nothing queued, as before
    (err,) = summary["worker_errors"]       # …but the reason is on the record
    assert err["processor"] == "capture" and "429" in err["error"]

    # The dump is intact and re-armed. Load-bearing: the pass must NOT
    # memoize the vault fingerprint, or the next pass reads "vault
    # unchanged", skips the scan, and the text sits there forever — the
    # failure has to cost a delay, never the text. Nothing edits the vault
    # between the two passes, so this only works if the memo was dropped.
    assert "buy milk" in (vault / "Harness/Capture.md").read_text()
    w2 = make_watcher(vault, capture_extractor=fake_extractor, disabled=OTHERS)
    summary2 = asyncio.run(w2.run_once())
    assert summary2["scanned"] is True      # the memo did not suppress it
    assert summary2["drafted"] == 1 and summary2["worker_errors"] == []


def test_a_quiet_pass_records_no_worker_error(vault, make_watcher):
    """The other half of the distinction: a genuinely empty extraction is
    not a failure, so nothing must claim the worker is down."""
    async def finds_nothing(text, today):
        return []

    (vault / "Harness/Capture.md").write_text(CAPTURE_SKELETON + "\njust a status note\n")
    w = make_watcher(vault, capture_extractor=finds_nothing, disabled=OTHERS)
    summary = asyncio.run(w.run_once())
    assert summary["drafted"] == 0 and summary["worker_errors"] == []


def test_a_processor_whose_scan_throws_does_not_memoize_the_tree(
        vault, make_watcher):
    """The dead-worker rule is about the MEMO, not about workers: a
    processor whose scan raised never read the tree either, so the pass
    cannot vouch for it. This shipped uncovered — the memo gate was
    spelled per-failure and only the draft path had been wired to it, so
    a poison scan memoized "handled" over notes nothing had looked at."""
    from tezcat.watcher import ProcessorSpec

    def poison_scan(w):
        raise RuntimeError("a note vanished mid-walk")

    (vault / "Harness/Capture.md").write_text(CAPTURE_SKELETON + "\ncall the vet\n")
    w = make_watcher(vault, registry=(ProcessorSpec(name="boom", scan=poison_scan,
                                                    draft=None),))
    summary = asyncio.run(w.run_once())

    assert summary["incomplete"] == ["scan:boom"]
    assert json.loads(w.state_path.read_text())["fingerprint"] == {}


def test_a_clean_pass_is_the_only_one_that_memoizes(vault, make_watcher):
    """The other half: nothing gave up, so the memo is earned. Without
    this the gate could be satisfied by never memoizing at all."""
    (vault / "Harness/Capture.md").write_text(CAPTURE_SKELETON + "\ncall the vet\n")
    w = make_watcher(vault, capture_extractor=fake_extractor, disabled=OTHERS)
    summary = asyncio.run(w.run_once())

    assert summary["incomplete"] == []
    assert json.loads(w.state_path.read_text())["fingerprint"]


def test_rescan_forgets_the_memo_without_forgetting_the_seen_hashes(
        vault, make_watcher):
    """`tezcat-watch --rescan`: the memo is a pure optimization and goes,
    but the seen-hashes are real dedup state and must survive — a fresh
    Watcher's in-memory `seen` is empty, so writing it blindly would
    re-draft every surface's last text."""
    (vault / "Harness/Capture.md").write_text(CAPTURE_SKELETON + "\ncall the vet\n")
    w = make_watcher(vault, capture_extractor=fake_extractor, disabled=OTHERS)
    asyncio.run(w.run_once())
    saved = json.loads(w.state_path.read_text())
    assert saved["fingerprint"] and saved["seen"].get("capture")

    fresh = make_watcher(vault, capture_extractor=fake_extractor, disabled=OTHERS)
    fresh.forget_vault_memo()
    after = json.loads(fresh.state_path.read_text())
    assert after["fingerprint"] == {}                       # memo dropped
    assert after["seen"] == saved["seen"]                   # dedup intact
