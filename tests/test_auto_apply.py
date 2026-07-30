"""Verified-safe records apply without asking (the `verified` policy +
verify_record, the deterministic fidelity gate); anything the verifier
can't vouch for waits in the queue for a human verdict."""

import asyncio
import datetime as _dt
import json as _json
from pathlib import Path

import pytest

from tests.conftest import record_worker_returning
from chickenskratch import proposals
from chickenskratch.processors import CAPTURE_SKELETON

TODAY = _dt.date.today().isoformat()


@pytest.fixture()
def vault(gitvault):
    return gitvault


faithful = record_worker_returning(
    [{"text": "buy a car", "due": None, "priority": None, "owed_by": None}])
inventing = record_worker_returning(
    [{"text": "hire a butler", "due": None, "priority": None, "owed_by": None}])


def run(w):
    return asyncio.run(w.run_once())


def filed_note(vault):
    return vault / "Captures" / f"{TODAY}.md"


FOCUS = ("schema_fix", "grouping", "spec_drift")  # isolate the capture lane


def dump(vault, text):
    (vault / "Harness/Capture.md").write_text(f"# Capture\n\n{text}\n")


def test_faithful_extraction_applies_without_checkbox(vault, make_watcher):
    w = make_watcher(vault, record_worker=faithful, disabled=FOCUS, n_max=20)
    dump(vault, "buy a car")
    summary = run(w)
    assert summary["auto"] == 1
    after = filed_note(vault).read_text()
    assert f"- [ ] buy a car ➕ {TODAY} ^t-" in after
    assert "> buy a car" in after                          # verbatim, quoted
    assert proposals.pending(w.proposals_dir) == []
    (row,) = [r for r in proposals.load_all(w.proposals_dir) if r["kind"] == "capture"]
    assert row["status"] == "auto"
    # the surface cleared through the on_applied hook — auto or approved,
    # same bookkeeping
    assert (vault / "Harness/Capture.md").read_text() == CAPTURE_SKELETON


def test_invented_words_fall_back_to_the_queue(vault, make_watcher):
    w = make_watcher(vault, record_worker=inventing, disabled=FOCUS, n_max=20)
    dump(vault, "buy a car")
    summary = run(w)
    assert summary["auto"] == 0
    (p,) = proposals.pending(w.proposals_dir)
    assert p["kind"] == "capture"  # still drafted — just needs the checkbox
    assert not filed_note(vault).exists()  # nothing landed without consent


def test_spec_policy_can_revoke_the_trust(vault, make_watcher):
    (vault / "Harness").mkdir(exist_ok=True)
    (vault / "Harness/Spec.md").write_text(
        "---\npolicies: {capture: ask}\n---\n\n# Vault Spec\n")
    w = make_watcher(vault, record_worker=faithful, disabled=FOCUS, n_max=20)
    dump(vault, "buy a car")
    summary = run(w)
    assert summary["auto"] == 0
    (p,) = proposals.pending(w.proposals_dir)
    assert p["kind"] == "capture"


def test_grammar_and_punctuation_improvements_auto_apply(vault, make_watcher):
    polished = record_worker_returning([
        {"text": "Buy a car.", "due": None, "priority": None, "owed_by": None},
        {"text": "Follow up with Alex about the Northwind requests",
         "due": None, "priority": None, "owed_by": None},
    ])
    w = make_watcher(vault, record_worker=polished, disabled=FOCUS, n_max=20)
    dump(vault, "buy a car\nfollow up with alex about northwind requests")
    summary = run(w)
    assert summary["auto"] == 1  # capitalization, period, and "the" all allowed
    after = filed_note(vault).read_text()
    assert f"- [ ] Buy a car. ➕ {TODAY} ^t-" in after
    assert "Follow up with Alex about the Northwind requests" in after


def test_negation_never_passes_any_distance(vault, make_watcher):
    negated = record_worker_returning(
        [{"text": "Don't buy a car", "due": None, "priority": None, "owed_by": None}])
    w = make_watcher(vault, record_worker=negated, disabled=FOCUS, n_max=20)
    dump(vault, "buy a car")
    summary = run(w)
    assert summary["auto"] == 0  # ~92% similar, 100% wrong — checkbox required
    (p,) = proposals.pending(w.proposals_dir)
    assert p["kind"] == "capture"


def test_word_mashup_fails_sequence_coverage(vault, make_watcher):
    mashup = record_worker_returning(
        [{"text": "detections buy the car test", "due": None,
          "priority": None, "owed_by": None}])
    w = make_watcher(vault, record_worker=mashup, disabled=FOCUS, n_max=20)
    dump(vault, "buy a car\ntest the detections")
    summary = run(w)
    assert summary["auto"] == 0  # every word is the user's; the sentence is not


def test_strict_fidelity_queues_grammar_edits(vault, make_watcher):
    (vault / "Harness").mkdir(exist_ok=True)
    (vault / "Harness/Spec.md").write_text("---\nfidelity: strict\n---\n\n# Vault Spec\n")
    polished = record_worker_returning(
        [{"text": "Follow up with Alex about the Northwind requests",
          "due": None, "priority": None, "owed_by": None}])
    w = make_watcher(vault, record_worker=polished, disabled=FOCUS, n_max=20)
    dump(vault, "follow up with alex about northwind requests")
    summary = run(w)
    assert summary["auto"] == 0  # "the" is invented under strict
    (p,) = proposals.pending(w.proposals_dir)
    assert p["kind"] == "capture"


MEETING_NO_ID = """---
type: meeting
date: 2026-07-23
attendees: [me]
---

# Sync

- decided nothing
"""


def test_additive_schema_fix_applies_without_checkbox(vault, make_watcher):
    note = vault / "Meetings/Sync.md"
    note.write_text(MEETING_NO_ID)

    async def stamping_drafter(item):
        return {"title": "stamp", "summary": "id only",
                "apply": [{"tool": "update_frontmatter",
                           "args": {"path": item["note"], "key": "id", "value": item["new_id"]}}]}

    w = make_watcher(vault, drafter=stamping_drafter, n_max=20,
                     disabled=("capture", "spec_drift", "grouping"))
    summary = run(w)
    assert summary["auto"] >= 1
    auto_notes = [r["drafted_against"]["note"] for r in proposals.load_all(w.proposals_dir)
                  if r["status"] == "auto"]
    # the synthetic note lacked ONLY its id — additive stamp => conformant => auto
    assert "Meetings/Sync.md" in auto_notes
    fixed = (vault / "Meetings/Sync.md").read_text()
    assert "id: m-" in fixed
    assert "- decided nothing" in fixed  # user text untouched


def test_rewording_schema_fix_keeps_its_checkbox(vault, make_watcher):
    note = vault / "Meetings/Sync.md"
    note.write_text(MEETING_NO_ID)

    async def rewording_drafter(item):
        return {"title": "bad", "summary": "rewords",
                "apply": [
                    {"tool": "update_frontmatter",
                     "args": {"path": item["note"], "key": "id", "value": item["new_id"]}},
                    {"tool": "replace_section",
                     "args": {"path": item["note"], "heading": "Sync",
                              "content": "- we decided NOTHING at all"}},
                ]}

    w = make_watcher(vault, drafter=rewording_drafter, n_max=20,
                     disabled=("capture", "spec_drift", "grouping"))
    summary = run(w)
    assert summary["auto"] == 0  # altered user text: every fix queued
    assert len(proposals.pending(w.proposals_dir)) >= 1
    assert "- decided nothing" in note.read_text()  # untouched


def test_partial_schema_fix_queues_to_avoid_redraft_churn(vault, make_watcher):
    # missing id AND date: fixing only the id leaves it nonconforming
    note = vault / "Meetings/Sync.md"
    note.write_text("---\ntype: meeting\nattendees: [me]\n---\n\n# Sync\n\n- notes\n")

    async def partial_drafter(item):
        return {"title": "partial", "summary": "id only",
                "apply": [{"tool": "update_frontmatter",
                           "args": {"path": item["note"], "key": "id", "value": item["new_id"]}}]}

    w = make_watcher(vault, drafter=partial_drafter, n_max=20,
                     disabled=("capture", "spec_drift", "grouping"))
    summary = run(w)
    auto_syncs = [r for r in proposals.load_all(w.proposals_dir)
                  if r["status"] == "auto" and r["drafted_against"]["note"] == "Meetings/Sync.md"]
    assert auto_syncs == [] and summary["auto"] == 0


def test_schema_fix_aimed_at_another_note_is_never_a_proposal(vault, make_watcher):
    """The path pin: the worker authors full tool args, and a schema fix
    has no legitimate cross-note step — so a plan aimed anywhere but the
    drafted note is a malfunction that dies at draft time. It must not
    even queue: an inattentive approve would still execute it."""
    note = vault / "Meetings/Sync.md"
    note.write_text(MEETING_NO_ID)
    reading_before = (vault / "Tasks/Reading List.md").read_text()

    async def hijacking_drafter(item):
        return {"title": "stamp", "summary": "id only",
                "apply": [
                    {"tool": "update_frontmatter",
                     "args": {"path": item["note"], "key": "id", "value": item["new_id"]}},
                    # simulated against Sync.md this is an innocent added
                    # heading; applied for real it writes elsewhere
                    {"tool": "append_to_note",
                     "args": {"path": "Tasks/Reading List.md", "content": "# injected"}},
                ]}

    w = make_watcher(vault, drafter=hijacking_drafter, n_max=20,
                     disabled=("capture", "spec_drift", "grouping"))
    summary = run(w)
    assert summary["auto"] == 0
    assert (vault / "Tasks/Reading List.md").read_text() == reading_before
    assert "id: m-" not in note.read_text()
    # no proposal for Sync.md at all — the cross-note plan died at draft time
    assert [p for p in proposals.pending(w.proposals_dir)
            if p["drafted_against"]["note"] == "Meetings/Sync.md"] == []


def test_verify_pins_the_target_note_as_defense_in_depth():
    """Layer two of the path pin: even if a cross-note plan reached the
    auto-apply gate some other way, the verifier refuses to vouch."""
    from chickenskratch.processors import verify_schema_fix

    item = {"note": "Meetings/Sync.md", "content": MEETING_NO_ID}
    plan = {"apply": [{"tool": "append_to_note",
                       "args": {"path": "Tasks/Reading List.md", "content": "# x"}}]}
    failures = verify_schema_fix(None, item, plan)
    assert failures and "targets" in failures[0]


def test_dropped_worker_date_forces_the_checkbox(vault, make_watcher):
    """Faithful text but a lost date: the fidelity verifier can't see
    structured fields, so a sanitized-away impossible date must force
    the checkbox and say so in the issues list."""
    date_dropper = record_worker_returning(
        [{"text": "buy a car", "due": None, "priority": None,
          "owed_by": None, "dropped_due": "2026-13-01"}])
    w = make_watcher(vault, record_worker=date_dropper, disabled=FOCUS, n_max=20)
    dump(vault, "buy a car")
    summary = run(w)
    assert summary["auto"] == 0
    (p,) = proposals.pending(w.proposals_dir)
    assert p["needs_review"] is True
    assert any("2026-13-01" in i for i in p["issues"])


def test_schema_fix_suffixless_path_still_counts_as_the_target(vault, make_watcher):
    """Workers often drop the .md; resolve() treats the paths as the same
    note, so the pin must too — a legit fix must not queue over a suffix."""
    note = vault / "Meetings/Sync.md"
    note.write_text(MEETING_NO_ID)

    async def suffixless_drafter(item):
        return {"title": "stamp", "summary": "id only",
                "apply": [{"tool": "update_frontmatter",
                           "args": {"path": item["note"].removesuffix(".md"),
                                    "key": "id", "value": item["new_id"]}}]}

    w = make_watcher(vault, drafter=suffixless_drafter, n_max=20,
                     disabled=("capture", "spec_drift", "grouping"))
    summary = run(w)
    assert summary["auto"] >= 1
    assert "id: m-" in note.read_text()


def test_stale_capture_re_arms_instead_of_vanishing(vault, make_watcher):
    """The silent-drop bug, generalized (on_stale): a pending record
    proposal whose dump changes must flip stale AND clear its seen-hash,
    or the new text would never re-draft and the extraction would
    silently vanish."""
    (vault / "Harness").mkdir(exist_ok=True)
    (vault / "Harness/Spec.md").write_text(
        "---\npolicies: {capture: ask}\n---\n\n# Vault Spec\n")
    w = make_watcher(vault, record_worker=faithful, disabled=FOCUS, n_max=20)
    dump(vault, "buy a car")
    run(w)
    (p1,) = proposals.pending(w.proposals_dir)
    # the dump grows before the verdict: voids the pending draft
    dump(vault, "buy a car\nand also a boat")
    summary = run(w)
    assert summary["stale"] >= 1
    (p2,) = proposals.pending(w.proposals_dir)
    assert p2["kind"] == "capture" and p2["id"] != p1["id"]  # re-drafted, not dropped


# --------------- the policy levels, per lane ---------------


def _judgment_spec(needs_review=False):
    """A verifier-less processor (the judgment shape: grouping, overlap,
    plan_today) drafting one add_task step."""
    from chickenskratch.processors import ProcessorSpec

    def scan(w):
        return [{"kind": "judgment", "note": "Home.md", "vault_sha": w._pass_sha,
                 "content": (Path(w.vault_path) / "Home.md").read_text()}]

    async def draft(w, item):
        p = proposals.new_proposal(
            kind="judgment", note=item["note"], vault_sha=item["vault_sha"],
            note_content=item["content"], title="a judgment call", summary="s",
            issues=["pick"], apply=[{"tool": "add_task", "args": {
                "path": "Tasks/Judged.md", "text": "judged task"}}])
        if needs_review:
            p["needs_review"] = True
        return p

    return ProcessorSpec("judgment", scan, draft, render_diff=False)


def test_verified_policy_on_a_verifier_less_processor_still_asks(vault, make_watcher):
    """`verified` has teeth only where a deterministic check exists: on a
    judgment lane it behaves as ask — never a silent grant."""
    (vault / "Harness/Spec.md").write_text(
        "---\npolicies: {judgment: verified}\n---\n\n# Vault Spec\n")
    w = make_watcher(vault, registry=(_judgment_spec(),))
    summary = run(w)
    assert summary["drafted"] == 1 and summary["auto"] == 0
    assert len(proposals.pending(w.proposals_dir)) == 1
    assert not (vault / "Tasks/Judged.md").exists()


def test_full_policy_applies_the_judgment_lanes(vault, make_watcher):
    """`full` is the standing grant, per lane: a verifier-less processor
    applies without a checkbox — one revertible snapshot, resolved
    `auto`, nothing pending."""
    (vault / "Harness/Spec.md").write_text(
        "---\npolicies: {judgment: full}\n---\n\n# Vault Spec\n")
    w = make_watcher(vault, registry=(_judgment_spec(),))
    summary = run(w)
    assert summary["auto"] == 1
    assert proposals.pending(w.proposals_dir) == []
    assert "judged task" in (vault / "Tasks/Judged.md").read_text()
    from chickenskratch_vault.vault import Vault
    assert any("Judged" in c["subject"] for c in Vault(vault).history(5))


def test_full_policy_still_respects_needs_review_and_unlisted_lanes(vault, make_watcher):
    """The grant is not a bypass: a drafter-flagged proposal queues even
    at `full`, and a lane the map doesn't grant stays at its default."""
    (vault / "Harness/Spec.md").write_text(
        "---\npolicies: {judgment: full}\n---\n\n# Vault Spec\n")
    w = make_watcher(vault, registry=(_judgment_spec(needs_review=True),))
    assert run(w)["auto"] == 0
    assert len(proposals.pending(w.proposals_dir)) == 1

    (vault / "Harness/Spec.md").write_text("---\nn_max: 5\n---\n\n# Vault Spec\n")
    w2 = make_watcher(vault, registry=(_judgment_spec(),),
                      proposals_dir=w.proposals_dir.parent / "p2",
                      state_path=w.proposals_dir.parent / "s2.json")
    (vault / "Home.md").write_text((vault / "Home.md").read_text() + "nudge\n")
    assert run(w2)["auto"] == 0        # judgment defaults to ask
    assert len(proposals.pending(w2.proposals_dir)) == 1


def test_full_policy_outranks_a_failing_verifier_and_records_it(vault, make_watcher):
    """Under the per-lane standing grant a failing verifier no longer
    queues — the record files with the verifier's objections persisted
    on the resolved proposal, so the audit trail names exactly what
    nobody vouched for."""
    from chickenskratch import processors as procs

    (vault / "Harness/Spec.md").write_text(
        "---\npolicies: {capture: full}\n---\n\n# Vault Spec\n")
    dump(vault, "renew the domain")
    inventing_worker = record_worker_returning(
        [{"text": "renew the quarterly domain", "due": None,
          "scheduled": None, "priority": None, "owed_by": None}])
    w = make_watcher(vault, registry=(procs.CAPTURE,), record_worker=inventing_worker)
    summary = run(w)
    assert summary["auto"] == 1 and proposals.pending(w.proposals_dir) == []
    assert "renew the quarterly domain" in filed_note(vault).read_text()
    (rec,) = [_json.loads(f.read_text()) for f in w.proposals_dir.glob("p-*.json")]
    assert rec["status"] == "auto"
    assert any("invented word" in x for x in rec["verifier_failures"])


def test_policies_grant_autonomy_per_lane(vault, make_watcher):
    """The policy map is per-processor: capture at `full` files a
    summarized record on its own, while plan_today at `ask` still queues
    its picks — no global switch flipped, no conflicting second knob."""
    from chickenskratch import processors as procs

    (vault / "Harness/Spec.md").write_text(
        "---\npolicies: {capture: full, plan_today: ask}\n---\n\n# Vault Spec\n")
    dump(vault, "buy a car")
    (vault / "Tasks/Backlog.md").write_text(
        "# Backlog\n\n- [ ] plan me ^t-abcdefabcdef\n")
    worker = record_worker_returning(
        [{"text": "buy a car", "due": None, "priority": None, "owed_by": None}],
        summary="The user intends to buy a car.")

    async def eager_planner(item):
        return {"picks": [{"id": "t-abcdefabcdef", "reason": "why not"}]}

    w = make_watcher(vault, registry=(procs.CAPTURE, procs.PLAN_TODAY),
                     record_worker=worker, plan_drafter=eager_planner, n_max=10)
    summary = run(w)
    assert summary["auto"] == 1                      # the record, not the plan
    assert "The user intends to buy a car." in filed_note(vault).read_text()
    (p,) = proposals.pending(w.proposals_dir)
    assert p["kind"] == "plan_today"                 # judgment still asks


def test_full_policy_files_a_summarized_record_with_the_objection(vault, make_watcher):
    """The autonomous posture end to end: the prose objection rides
    verifier_failures on the resolved proposal, the folded verbatim is
    the human check, and the apply is one revertible snapshot."""
    from chickenskratch import processors as procs

    (vault / "Harness/Spec.md").write_text(
        "---\npolicies: {capture: full}\n---\n\n# Vault Spec\n")
    dump(vault, "buy a car")
    worker = record_worker_returning(
        [{"text": "buy a car", "due": None, "priority": None, "owed_by": None}],
        summary="The user intends to buy a car.")
    w = make_watcher(vault, registry=(procs.CAPTURE,), record_worker=worker)
    assert run(w)["auto"] == 1
    filed = filed_note(vault).read_text()
    assert "The user intends to buy a car." in filed
    assert "> buy a car" in filed                     # the check, one unfold away
    (rec,) = [_json.loads(f.read_text()) for f in w.proposals_dir.glob("p-*.json")]
    assert rec["status"] == "auto" and rec["undo"]
    assert any("summary prose" in x for x in rec["verifier_failures"])


def test_auto_applies_record_their_own_undo(vault, make_watcher):
    """The autonomy audit loop: an auto-applied record carries the
    harness commits it made, and reverting them reverts the decision —
    without hunting the git log."""
    from chickenskratch import service

    w = make_watcher(vault, record_worker=faithful, disabled=FOCUS, n_max=20)
    dump(vault, "buy a car")
    assert run(w)["auto"] == 1
    (rec,) = [_json.loads(f.read_text()) for f in w.proposals_dir.glob("p-*.json")]
    assert rec["status"] == "auto" and rec["undo"]
    from chickenskratch_vault.vault import Vault
    subjects = {c["sha"]: c["subject"] for c in Vault(vault).history(10)}
    assert all(subjects[sha].startswith("harness:") for sha in rec["undo"])

    assert "buy a car ➕" in filed_note(vault).read_text()  # the stamped apply
    for sha in rec["undo"]:
        service.revert(str(vault), sha)
    filed = filed_note(vault)
    assert not filed.exists() or "buy a car ➕" not in filed.read_text()
