"""The vault-side capture surface:
Harness/Capture.md → record decomposition → record proposal in the
queue → approve files ONE record note (tasks inside it, verbatim dump
quoted beneath) and clears the surface. A scripted fake record worker
stands in for the worker LLM."""

import asyncio
import datetime as dt
import json
import re

import pytest

import chickenskratch_vault.vault as vault_mod
from chickenskratch import proposals
from chickenskratch import watcher as watcher_mod
from chickenskratch.processors import CAPTURE_SKELETON
from chickenskratch_vault import tasks as tk
from chickenskratch_vault.vault import Vault
from tests.conftest import record_worker_returning

EXTRACTED = [
    {"text": "Migrate the DNS records", "due": "2026-07-24", "priority": "high"},
    {"text": "Revisit the pricing page", "due": None, "priority": None},
]

TODAY = dt.date.today().isoformat()

# every processor but capture: these tests are about the record path
OTHERS = ("schema_fix", "grouping", "spec_drift", "overlap", "plan_today")


fake_record_worker = record_worker_returning(EXTRACTED)


async def fake_schema_drafter(item):
    return {
        "title": f"Normalize {item['note']}",
        "summary": "stamp",
        "apply": [{"tool": "update_frontmatter",
                   "args": {"path": item["note"], "key": "id", "value": item["new_id"]}}],
    }


@pytest.fixture()
def vault(gitvault):
    # this suite tests the checkbox path — set the capture lane to ask
    (gitvault / "Harness").mkdir(exist_ok=True)
    (gitvault / "Harness/Spec.md").write_text(
        "---\npolicies: {capture: ask}\n---\n\n# Vault Spec\n")
    return gitvault


@pytest.fixture()
def watcher(vault, make_watcher):
    return make_watcher(vault, drafter=fake_schema_drafter,
                        record_worker=fake_record_worker, n_max=10)


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


def test_typed_text_becomes_a_record_proposal(vault, watcher):
    run(watcher)
    type_into_capture(vault, "\ncall the vendor about DNS, urgent, and pricing page someday\n")
    run(watcher)
    rows = capture_rows(watcher)
    assert len(rows) == 1
    p = rows[0]
    assert p["drafted_against"]["note"] == "Harness/Capture.md"
    # ONE atomic step: the record content and its filing are never
    # separately deselectable — "clear without filing" was the data
    # loss phase 0 exists to stop
    (step,) = p["apply"]
    assert step["tool"] == "append_to_note"
    assert step["args"]["path"] == f"Captures/{TODAY}.md"
    content = step["args"]["content"]
    assert "- [ ] Migrate the DNS records ⏫ 📅 2026-07-24" in content
    # the verbatim rides in, quoted, collapsed
    assert "> [!NOTE]- Verbatim" in content
    assert "> call the vendor about DNS, urgent, and pricing page someday" in content
    # the proposal carries its tasks as issues — what the dashboard renders
    assert "Migrate the DNS records — due 2026-07-24, high" in p["issues"]
    from chickenskratch.processors import REGISTRY
    assert not next(s for s in REGISTRY if s.name == "capture").render_diff


def test_prose_only_dump_still_becomes_a_record(vault, make_watcher):
    """A dump with no tasks is not "nothing extractable" — it is a
    record with nothing to do. The whole point of the record lane: notes
    stop going missing just because they contain no work."""
    worker = record_worker_returning(
        [], summary="Alex thinks the pipeline needs outsourcing.")
    (vault / "Harness/Capture.md").write_text(
        CAPTURE_SKELETON + "\nthe pipeline can't be resolved without outsourcing - alex\n")
    w = make_watcher(vault, record_worker=worker, disabled=OTHERS)
    summary = asyncio.run(w.run_once())
    assert summary["drafted"] == 1 and summary["worker_errors"] == []
    (p,) = capture_rows(w)
    content = p["apply"][0]["args"]["content"]
    assert "Alex thinks the pipeline needs outsourcing." in content
    assert "> the pipeline can't be resolved" in content


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


def test_approve_files_the_record_and_clears_the_surface(vault, watcher, monkeypatch):
    recorded = []
    monkeypatch.setattr("chickenskratch.watcher.record_dashboard",
                        lambda p, d, **k: recorded.append((p["kind"], d)))
    run(watcher)
    type_into_capture(vault, "\nmigrate dns and revisit pricing\n")
    run(watcher)
    (p,) = capture_rows(watcher)

    summary = asyncio.run(watcher.run_once(decisions=[(p['id'], "approve")]))

    assert summary["approved"] == 1
    filed = (vault / "Captures" / f"{TODAY}.md").read_text()
    assert "type: capture" in filed                        # born typed
    assert "Migrate the DNS records ⏫ 📅 2026-07-24" in filed
    assert "Revisit the pricing page" in filed
    assert "> migrate dns and revisit pricing" in filed    # verbatim, quoted
    assert (vault / "Harness/Capture.md").read_text() == CAPTURE_SKELETON
    assert ("capture", "approved") in recorded
    assert any("clear Harness/Capture.md" in c["subject"] for c in Vault(vault).history())


def test_owed_items_stay_in_the_record_marked_with_owner(vault, watcher):
    watcher.record_worker = record_worker_returning([
        {"text": "Review the vendor contract", "due": "2026-08-05", "priority": None, "owed_by": None},
        {"text": "Send the vendor contract", "due": None, "priority": None, "owed_by": "Nadia"},
    ])
    run(watcher)
    type_into_capture(vault, "\nnadia will send the contract; review it in two weeks\n")
    run(watcher)
    (p,) = capture_rows(watcher)
    assert "1 task(s)" in p["title"] and "1 waiting-on item(s)" in p["title"]
    content = p["apply"][0]["args"]["content"]
    # owed-ness is the 👤 field on the line, not a folder
    assert "- [ ] Send the vendor contract 👤 Nadia" in content
    assert "waiting on Nadia: Send the vendor contract" in p["issues"]

    summary = asyncio.run(watcher.run_once(decisions=[(p['id'], "approve")]))
    assert summary["approved"] == 1
    filed = (vault / "Captures" / f"{TODAY}.md").read_text()
    owed = [t for t in tk.iter_tasks(filed) if t.owner]
    assert [t.owner for t in owed] == ["Nadia"]
    # nothing routes to the retired waiting inbox
    assert not (vault / "Tasks/Waiting.md").exists()
    assert (vault / "Harness/Capture.md").read_text() == CAPTURE_SKELETON


def test_meeting_dump_becomes_a_meeting_record(vault, watcher):
    watcher.record_worker = record_worker_returning(
        [{"text": "research azure ci/cd", "due": None, "priority": None, "owed_by": None},
         {"text": "pipeline costs", "due": None, "priority": None, "owed_by": "Nadia"}],
        kind="meeting", title="team weekly", attendees=("Alex", "Nadia"),
        summary="Alex wants a baseline push before the Azure migration.")
    run(watcher)
    type_into_capture(vault, "\nmeeting w team abt azure. alex sez baseline push. "
                             "action item: research azure ci/cd. nadia owes pipeline costs\n")
    run(watcher)
    (p,) = capture_rows(watcher)
    (step,) = p["apply"]
    assert step["tool"] == "create_note"
    assert step["args"]["path"] == f"Meetings/{TODAY} team weekly.md"

    summary = asyncio.run(watcher.run_once(decisions=[(p['id'], "approve")]))
    assert summary["approved"] == 1
    note = (vault / "Meetings" / f"{TODAY} team weekly.md").read_text()
    assert "type: meeting" in note and "id: m-" in note
    assert "- Alex" in note and "- Nadia" in note          # attendees frontmatter
    assert "## Summary" in note and "baseline push" in note
    assert "## Action items" in note and "## Waiting on" in note
    assert "- [ ] pipeline costs 👤 Nadia" in note
    assert "> [!NOTE]- Verbatim" in note
    assert "> meeting w team abt azure" in note
    assert (vault / "Harness/Capture.md").read_text() == CAPTURE_SKELETON


def test_quoted_verbatim_is_inert_to_the_task_walk(vault, watcher):
    """The revision-3 regression: a dump containing literal checkbox
    lines is filed verbatim AND extracted — without the quoting, both
    copies would be walked as open tasks (and overlap couldn't merge
    them: the filed copy has no block id). The blockquote is the
    mechanism that makes the verbatim inert."""
    watcher.record_worker = record_worker_returning(
        [{"text": "fix the pipeline", "due": None, "priority": None, "owed_by": None}])
    run(watcher)
    type_into_capture(vault, "\n- [ ] fix the pipeline\nsome context about why\n")
    run(watcher)
    (p,) = capture_rows(watcher)
    asyncio.run(watcher.run_once(decisions=[(p['id'], "approve")]))

    filed = (vault / "Captures" / f"{TODAY}.md").read_text()
    assert "> - [ ] fix the pipeline" in filed             # verbatim, quoted
    open_tasks = [t for t in tk.iter_tasks(filed) if not t.done]
    assert len(open_tasks) == 1                            # the extracted copy only
    assert open_tasks[0].block_id                          # …the stamped one


def test_worker_down_completes_the_pass(vault, watcher):
    """A dead record worker fails capture's own drafts (retried next
    pass, no proposal enters the queue) but no longer halts the pass or
    the processors behind it — see the //commands starvation fix."""
    async def down(text, today):
        raise vault_mod.VaultError("worker unavailable")

    watcher.record_worker = down
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
    from chickenskratch_vault.vault import VaultError

    async def dead_worker(text, today):
        raise VaultError("extract_record worker (some-model) unavailable: 429")

    (vault / "Harness/Capture.md").write_text(
        CAPTURE_SKELETON + "\nbuy milk and call the vet tomorrow\n")
    w = make_watcher(vault, record_worker=dead_worker, disabled=OTHERS)
    summary = asyncio.run(w.run_once())

    assert summary["drafted"] == 0          # nothing queued, as before
    (err,) = summary["worker_errors"]       # …but the reason is on the record
    assert err["processor"] == "capture" and "429" in err["error"]

    # The dump is intact and re-armed: the failure costs a delay, never
    # the text. Nothing edits the vault between the two passes, so this
    # only works because the seen-hash is stamped past a SUCCESSFUL draft
    # and the next pass scans regardless.
    assert "buy milk" in (vault / "Harness/Capture.md").read_text()
    w2 = make_watcher(vault, record_worker=fake_record_worker, disabled=OTHERS)
    summary2 = asyncio.run(w2.run_once())
    assert summary2["drafted"] == 1 and summary2["worker_errors"] == []


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
        w = make_watcher(vault, record_worker=fake_record_worker, disabled=OTHERS)
        (err,) = asyncio.run(w.run_once())["worker_errors"]
        assert err["processor"] == "capture" and "invalid plan" in err["error"]
        state = json.loads(w.state_path.read_text())
        parked = attempt == watcher_mod.DRAFT_PLAN_ATTEMPTS
        # while budget remains the seen-hash is dropped, so the next pass
        # redrafts; once parked it stays stamped and suppresses the retry
        assert err["retrying"] is not parked
        assert bool(state["seen"].get("capture")) is parked
    # parked: the slot entry persists at the ceiling — the entry ITSELF is
    # the parking, because the deterministic lanes (schema_fix, grouping)
    # re-find their items every pass and stamp no seen-hash for a parking
    # to ride on. _save_state bounds the map so it never grows a scrapheap.
    ((slot, spent),) = json.loads(w.state_path.read_text())["draft_attempts"].items()
    assert slot.startswith("capture:") and spent == watcher_mod.DRAFT_PLAN_ATTEMPTS
    seen_hash = json.loads(w.state_path.read_text())["seen"]["capture"]

    # a further pass no longer calls the model at all — the seen-hash holds
    w = make_watcher(vault, record_worker=fake_record_worker, disabled=OTHERS)
    assert asyncio.run(w.run_once())["worker_errors"] == []
    assert json.loads(w.state_path.read_text())["seen"]["capture"] == seen_hash

    # editing the text is the documented lever: new hash, fresh budget
    monkeypatch.undo()
    (vault / "Harness/Capture.md").write_text(CAPTURE_SKELETON + "\ncall the vet back\n")
    w = make_watcher(vault, record_worker=fake_record_worker, disabled=OTHERS)
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

    w = make_watcher(vault, record_worker=fake_record_worker, disabled=OTHERS)
    asyncio.run(w.run_once())
    assert json.loads(w.state_path.read_text())["draft_attempts"]  # one in flight

    w2 = make_watcher(vault, record_worker=fake_record_worker, disabled=OTHERS)
    assert asyncio.run(w2.run_once())["drafted"] == 1     # the retry lands
    assert json.loads(w2.state_path.read_text())["draft_attempts"] == {}


def test_a_poison_scan_does_not_strand_the_text_behind_it(vault, make_watcher):
    """A processor whose scan raises fails ITS processor, never the pass —
    and never the input. This is the case the vault memo used to break:
    the pass memoized "this tree is handled" over notes nothing had read,
    and the capture sat there until an unrelated edit moved the
    fingerprint. Now every pass scans, so it is structural."""
    from chickenskratch.watcher import ProcessorSpec

    def poison_scan(w):
        raise RuntimeError("a note vanished mid-walk")

    (vault / "Harness/Capture.md").write_text(CAPTURE_SKELETON + "\ncall the vet\n")
    boom = ProcessorSpec(name="boom", scan=poison_scan, draft=None)
    w = make_watcher(vault, record_worker=fake_record_worker,
                     registry=(boom, *(p for p in watcher_mod.processors.REGISTRY
                                       if p.name == "capture")))
    # the poison processor took itself down; capture behind it still drafted
    assert asyncio.run(w.run_once())["drafted"] == 1


def test_state_carries_only_what_it_has_to(vault, make_watcher):
    """Two keys, both real dedup state. The vault fingerprint used to live
    here as a third — a pure optimization that cost an entire class of
    stranded-input failure. It is not coming back by accident."""
    (vault / "Harness/Capture.md").write_text(CAPTURE_SKELETON + "\ncall the vet\n")
    w = make_watcher(vault, record_worker=fake_record_worker, disabled=OTHERS)
    asyncio.run(w.run_once())
    assert set(json.loads(w.state_path.read_text())) == {"seen", "draft_attempts"}


def test_a_second_dump_appends_to_the_same_day(vault, make_watcher):
    """One capture note per day, not one per dump — the day is the record."""
    worker = record_worker_returning(
        [{"text": "call the vendor", "due": None, "priority": None, "owed_by": None}])

    note = vault / "Captures" / f"{TODAY}.md"
    for text in ("call the vendor, pipeline needs outsourcing",
                 "call the vendor, baseline push first"):
        (vault / "Harness/Capture.md").write_text(CAPTURE_SKELETON + "\n" + text + "\n")
        w = make_watcher(vault, record_worker=worker, disabled=OTHERS)
        asyncio.run(w.run_once())
        (p,) = capture_rows(w)
        asyncio.run(w.run_once(decisions=[(p["id"], "approve")], lock_wait=5))

    filed = note.read_text()
    assert "> call the vendor, pipeline needs outsourcing" in filed
    assert "> call the vendor, baseline push first" in filed
    assert filed.count("## ") == 2          # one timestamped block per dump
    assert filed.count("type: capture") == 1  # frontmatter rides only the first


def test_a_titled_dump_names_its_block_and_the_title_is_checked(vault, watcher):
    """A capture block's heading carries the dump's subject ("## 14:32 —
    azure costs") — the rail's day labels read these back. The title is
    model text, so verify_record holds it to task fidelity: words from
    the dump pass; an invented subject fails the check."""
    from chickenskratch.processors import verify_record

    watcher.record_worker = record_worker_returning(
        [{"text": "review the azure costs", "due": None, "priority": None,
          "owed_by": None}],
        title="azure costs")
    run(watcher)
    type_into_capture(vault, "\nreview the azure costs before the sync\n")
    run(watcher)
    (p,) = capture_rows(watcher)
    content = p["apply"][0]["args"]["content"]
    assert re.search(r"^## \d{2}:\d{2} — azure costs$", content, re.M)
    item = {"text": "review the azure costs before the sync"}
    assert verify_record(watcher, item, p) == []          # title traces to the dump

    invented = {**p, "apply": [{**p["apply"][0], "args": {
        **p["apply"][0]["args"],
        "content": content.replace("— azure costs", "— quarterly budget review"),
    }}]}
    assert any("quarterly" in f for f in verify_record(watcher, item, invented))


def test_the_record_plan_is_one_atomic_step(vault, watcher):
    """Filing the record and its content are never separately
    deselectable: the successor to "archiving and clearing are one step
    on purpose". A partial approval of a capture is therefore impossible
    by construction — approve files everything, reject files nothing —
    and the declined-text-cleared bug class is closed structurally."""
    run(watcher)
    type_into_capture(vault, "\nmigrate dns and revisit pricing\n")
    run(watcher)
    (p,) = capture_rows(watcher)
    assert len(p["apply"]) == 1

    # rejecting leaves the surface untouched — the dump is still the only copy
    asyncio.run(watcher.run_once(decisions=[(p["id"], "reject")]))
    assert "migrate dns" in (vault / "Harness/Capture.md").read_text()
    assert not (vault / "Captures" / f"{TODAY}.md").exists()


def test_a_config_declared_kind_files_itself_with_zero_code(
        vault, make_watcher, tmp_path, monkeypatch):
    """The kind-driven promise: a schema file with a record block is a
    complete new capture behavior — folder, id prefix, frontmatter, and
    the extract_record enum all follow from config, no Python."""
    import shutil

    from chickenskratch.bootstrap import ROOT
    from chickenskratch_vault import workers as wk

    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    (cfg / "schemas" / "standup.yaml").write_text(
        "id_prefix: s\n"
        "record:\n"
        "  kind: standup\n"
        "  files_as: note\n"
        "  dir_key: meetings_dir\n"
        "  cues: |\n"
        "    - kind \"standup\" when the dump is a per-person status round-up.\n"
        "type: object\n"
        "required: [id, type, date]\n"
        "properties:\n"
        "  id: {type: string}\n"
        "  type: {const: standup}\n"
        "  date: {type: string}\n"
        "  attendees:\n"
        "    type: array\n"
        "    items: {type: string}\n",
        encoding="utf-8")
    monkeypatch.setenv("VAULT_CONFIG_PATH", str(cfg / "vault.yaml"))

    # the composed worker already offers the new kind — config, not code
    spec = wk.get_worker("extract_record")
    assert "standup" in spec.schema["properties"]["record"]["properties"]["kind"]["enum"]
    assert 'kind "standup" when the dump' in spec.prompt

    w = make_watcher(vault, drafter=fake_schema_drafter, n_max=10,
                     record_worker=record_worker_returning(
                         [{"text": "unblock the deploy", "due": None,
                           "priority": None, "owed_by": None}],
                         kind="standup", title="daily standup",
                         attendees=("Alex", "Rio"), summary="Alex is blocked."))
    run(w)
    type_into_capture(vault, "\nstandup: alex blocked on deploy, rio unblock the deploy\n")
    run(w)
    (p,) = capture_rows(w)
    (step,) = p["apply"]
    assert step["tool"] == "create_note"
    assert step["args"]["path"] == f"Meetings/{TODAY} daily standup.md"

    summary = asyncio.run(w.run_once(decisions=[(p["id"], "approve")]))
    assert summary["approved"] == 1
    note = (vault / "Meetings" / f"{TODAY} daily standup.md").read_text()
    assert "type: standup" in note and "id: s-" in note   # its own type and prefix
    assert "- Alex" in note and "- Rio" in note           # schema declares attendees
    assert "> standup: alex blocked on deploy" in note    # verbatim rides along
