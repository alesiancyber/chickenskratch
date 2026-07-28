"""The vault-side capture surface:
Harness/Capture.md → extraction worker → capture proposal in the queue →
approve adds tasks to the inbox and clears the surface. A scripted fake
extractor stands in for the worker LLM."""

import asyncio
import json

import pytest

import tezcat_vault.vault as vault_mod
from tezcat import proposals
from tezcat import watcher as watcher_mod
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

    # The dump is intact and re-armed: the failure costs a delay, never
    # the text. Nothing edits the vault between the two passes, so this
    # only works because the seen-hash is stamped past a SUCCESSFUL draft
    # and the next pass scans regardless.
    assert "buy milk" in (vault / "Harness/Capture.md").read_text()
    w2 = make_watcher(vault, capture_extractor=fake_extractor, disabled=OTHERS)
    summary2 = asyncio.run(w2.run_once())
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


def test_an_unusable_plan_is_reported_and_retried_then_parked(vault, make_watcher,
                                                              monkeypatch):
    """The silent-loss case: extraction ran, the plan failed its schema,
    and the bare `continue` left BOTH suppressors engaged — seen-hash
    stamped and the memo saved — so no later pass could reach the text
    while every surface said "nothing extractable".

    It re-arms on a budget now: retried while there's a chance it was a
    flake, then parked, because the drafters own the proposal's shape and
    a plan rejected for this input tends to be rejected identically."""
    def reject(p, d):
        raise ValueError("invalid proposal: title too short")

    monkeypatch.setattr(proposals, "save", reject)
    (vault / "Harness/Capture.md").write_text(CAPTURE_SKELETON + "\ncall the vet\n")

    for attempt in range(1, watcher_mod.DRAFT_PLAN_ATTEMPTS + 1):
        w = make_watcher(vault, capture_extractor=fake_extractor, disabled=OTHERS)
        (err,) = asyncio.run(w.run_once())["worker_errors"]
        assert err["processor"] == "capture" and "invalid plan" in err["error"]
        state = json.loads(w.state_path.read_text())
        parked = attempt == watcher_mod.DRAFT_PLAN_ATTEMPTS
        # while budget remains the seen-hash is dropped, so the next pass
        # redrafts; once parked it stays stamped and suppresses the retry
        assert err["retrying"] is not parked
        assert bool(state["seen"].get("capture")) is parked
    # parked: the budget is spent AND released, never left as scrap
    assert json.loads(w.state_path.read_text())["draft_attempts"] == {}
    seen_hash = json.loads(w.state_path.read_text())["seen"]["capture"]

    # a further pass no longer calls the model at all — the seen-hash holds
    w = make_watcher(vault, capture_extractor=fake_extractor, disabled=OTHERS)
    assert asyncio.run(w.run_once())["worker_errors"] == []
    assert json.loads(w.state_path.read_text())["seen"]["capture"] == seen_hash

    # editing the text is the documented lever: new hash, fresh budget
    monkeypatch.undo()
    (vault / "Harness/Capture.md").write_text(CAPTURE_SKELETON + "\ncall the vet back\n")
    w = make_watcher(vault, capture_extractor=fake_extractor, disabled=OTHERS)
    assert asyncio.run(w.run_once())["drafted"] == 1


def test_a_plan_that_saves_retires_the_budget_its_last_one_burned(vault, make_watcher,
                                                                  monkeypatch):
    """A transient rejection must not leave a counter behind — the budget
    exists only while a retry is in flight."""
    calls = {"n": 0}
    real_save = proposals.save

    def flaky(p, d):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("invalid proposal: title too short")
        return real_save(p, d)

    monkeypatch.setattr(proposals, "save", flaky)
    (vault / "Harness/Capture.md").write_text(CAPTURE_SKELETON + "\ncall the vet\n")

    w = make_watcher(vault, capture_extractor=fake_extractor, disabled=OTHERS)
    asyncio.run(w.run_once())
    assert json.loads(w.state_path.read_text())["draft_attempts"]  # one in flight

    w2 = make_watcher(vault, capture_extractor=fake_extractor, disabled=OTHERS)
    assert asyncio.run(w2.run_once())["drafted"] == 1     # the retry lands
    assert json.loads(w2.state_path.read_text())["draft_attempts"] == {}


def test_a_poison_scan_does_not_strand_the_text_behind_it(vault, make_watcher):
    """A processor whose scan raises fails ITS processor, never the pass —
    and never the input. This is the case the vault memo used to break:
    the pass memoized "this tree is handled" over notes nothing had read,
    and the capture sat there until an unrelated edit moved the
    fingerprint. Now every pass scans, so it is structural."""
    from tezcat.watcher import ProcessorSpec

    def poison_scan(w):
        raise RuntimeError("a note vanished mid-walk")

    (vault / "Harness/Capture.md").write_text(CAPTURE_SKELETON + "\ncall the vet\n")
    boom = ProcessorSpec(name="boom", scan=poison_scan, draft=None)
    w = make_watcher(vault, capture_extractor=fake_extractor,
                     registry=(boom, *(p for p in watcher_mod.processors.REGISTRY
                                       if p.name == "capture")))
    # the poison processor took itself down; capture behind it still drafted
    assert asyncio.run(w.run_once())["drafted"] == 1


def test_state_carries_only_what_it_has_to(vault, make_watcher):
    """Two keys, both real dedup state. The vault fingerprint used to live
    here as a third — a pure optimization that cost an entire class of
    stranded-input failure. It is not coming back by accident."""
    (vault / "Harness/Capture.md").write_text(CAPTURE_SKELETON + "\ncall the vet\n")
    w = make_watcher(vault, capture_extractor=fake_extractor, disabled=OTHERS)
    asyncio.run(w.run_once())
    assert set(json.loads(w.state_path.read_text())) == {"seen", "draft_attempts"}


def test_approving_a_capture_files_the_dump_instead_of_deleting_it(vault, make_watcher):
    """The dump is a record, not just a source of tasks. Approving used to
    blank the whole surface, destroying the narrative around the tasks —
    it survived only in git and the proposal file, which is to say
    nowhere the user reads. The dump is filed, whole, then cleared.

    Deliberately not line surgery: a dump's dates and owners leave the
    text for structured fields, so no measure of "which lines were used"
    separates a consumed line from a prose one. Nothing is deleted."""
    async def one_task(text, today):
        return [{"text": "call the vendor about DNS", "due": None,
                 "priority": None, "owed_by": None}]

    (vault / "Harness/Capture.md").write_text(
        CAPTURE_SKELETON
        + "\ncall the vendor about DNS\n"
        + "\nthe pipeline can't be resolved without outsourcing - Alex thinks\n"
        + "a baseline push first, and the customer wants custom identity detection\n")
    w = make_watcher(vault, capture_extractor=one_task, disabled=OTHERS)
    asyncio.run(w.run_once())
    (p,) = capture_rows(w)
    assert "files the dump to Captures/" in p["summary"]   # said before approving

    asyncio.run(w.run_once(decisions=[(p["id"], "approve")], lock_wait=5))

    import datetime as dt
    filed = (vault / "Captures" / f"{dt.date.today().isoformat()}.md").read_text()
    assert "outsourcing" in filed and "custom identity detection" in filed
    assert "call the vendor about DNS" in filed        # the whole dump, not a remainder
    # …and the surface is clear for the next one
    assert (vault / "Harness/Capture.md").read_text() == CAPTURE_SKELETON
    assert "Review" not in filed


def test_a_second_dump_appends_to_the_same_day(vault, make_watcher):
    """One note per day, not one per dump — the day is the record."""
    async def one_task(text, today):
        return [{"text": "call the vendor", "due": None, "priority": None, "owed_by": None}]

    import datetime as dt
    note = vault / "Captures" / f"{dt.date.today().isoformat()}.md"
    for text in ("call the vendor, pipeline needs outsourcing",
                 "call the vendor, baseline push first"):
        (vault / "Harness/Capture.md").write_text(CAPTURE_SKELETON + "\n" + text + "\n")
        w = make_watcher(vault, capture_extractor=one_task, disabled=OTHERS)
        asyncio.run(w.run_once())
        (p,) = capture_rows(w)
        asyncio.run(w.run_once(decisions=[(p["id"], "approve")], lock_wait=5))

    filed = note.read_text()
    assert "outsourcing" in filed and "baseline push" in filed
    assert filed.count("## ") == 2          # one timestamped block per dump


def test_a_partial_approval_still_leaves_the_dump_alone(vault, make_watcher):
    """Nothing is filed or cleared while some of the dump's steps were
    declined — the text behind them is still the only copy."""
    from tezcat import service

    kw = dict(capture_extractor=fake_extractor, disabled=OTHERS)
    w = make_watcher(vault, **kw)
    (vault / "Harness/Capture.md").write_text(
        CAPTURE_SKELETON + "\nmigrate dns and revisit pricing\n")
    asyncio.run(w.run_once())
    (p,) = capture_rows(w)

    # only the first of two extracted tasks
    asyncio.run(service.decide(str(vault), p["id"], "approve", steps=[0],
                               proposals_dir=w.proposals_dir,
                               state_path=w.state_path, **kw))

    assert "migrate dns" in (vault / "Harness/Capture.md").read_text()
    import datetime as dt
    assert not (vault / "Captures" / f"{dt.date.today().isoformat()}.md").exists()
