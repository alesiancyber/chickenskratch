"""Verified-safe extraction applies without asking (Spec.md
`auto_apply` + the deterministic fidelity verifier); anything the
verifier can't vouch for waits in the queue for a human verdict."""

import asyncio
import datetime as _dt

import pytest

from harness import proposals
from harness.processors import CAPTURE_SKELETON

TODAY = _dt.date.today().isoformat()


@pytest.fixture()
def vault(gitvault):
    return gitvault


async def faithful(text, today):
    return [{"text": "buy a car", "due": None, "priority": None, "owed_by": None}]


async def inventing(text, today):
    return [{"text": "hire a butler", "due": None, "priority": None, "owed_by": None}]


def run(w):
    return asyncio.run(w.run_once())


FOCUS = ("schema_fix", "grouping", "spec_drift", "archive_done")  # isolate the extraction paths


def test_faithful_inbox_extraction_applies_without_checkbox(vault, make_watcher):
    w = make_watcher(vault, capture_extractor=faithful, disabled=FOCUS, n_max=20)
    inbox = vault / "Tasks/Inbox.md"
    inbox.write_text(inbox.read_text().rstrip("\n") + "\nbuy a car\n")
    summary = run(w)
    assert summary["auto"] == 1
    after = inbox.read_text()
    assert f"- [ ] buy a car ➕ {TODAY} ^t-" in after
    assert after.count("buy a car") == 1  # rewritten in place, not duplicated
    assert proposals.pending(w.proposals_dir) == []
    (row,) = [r for r in proposals.load_all(w.proposals_dir) if r["kind"] == "inbox_extract"]
    assert row["status"] == "auto"


def test_invented_words_fall_back_to_the_queue(vault, make_watcher):
    w = make_watcher(vault, capture_extractor=inventing, disabled=FOCUS, n_max=20)
    inbox = vault / "Tasks/Inbox.md"
    inbox.write_text(inbox.read_text().rstrip("\n") + "\nbuy a car\n")
    summary = run(w)
    assert summary["auto"] == 0
    (p,) = proposals.pending(w.proposals_dir)
    assert p["kind"] == "inbox_extract"  # still drafted — just needs the checkbox
    assert "hire a butler" not in inbox.read_text()  # nothing touched the note


def test_spec_note_can_revoke_the_trust(vault, make_watcher):
    (vault / "Harness").mkdir(exist_ok=True)
    (vault / "Harness/Spec.md").write_text("---\nauto_apply: []\n---\n\n# Vault Spec\n")
    w = make_watcher(vault, capture_extractor=faithful, disabled=FOCUS, n_max=20)
    inbox = vault / "Tasks/Inbox.md"
    inbox.write_text(inbox.read_text().rstrip("\n") + "\nbuy a car\n")
    summary = run(w)
    assert summary["auto"] == 0
    (p,) = proposals.pending(w.proposals_dir)
    assert p["kind"] == "inbox_extract"


def test_grammar_and_punctuation_improvements_auto_apply(vault, make_watcher):
    async def polished(text, today):
        return [
            {"text": "Buy a car.", "due": None, "priority": None, "owed_by": None},
            {"text": "Follow up with Alex about the Northwind requests",
             "due": None, "priority": None, "owed_by": None},
        ]

    w = make_watcher(vault, capture_extractor=polished, disabled=FOCUS, n_max=20)
    inbox = vault / "Tasks/Inbox.md"
    inbox.write_text(inbox.read_text().rstrip("\n")
                     + "\nbuy a car\nfollow up with alex about northwind requests\n")
    summary = run(w)
    assert summary["auto"] == 1  # capitalization, period, and "the" all allowed
    after = inbox.read_text()
    assert f"- [ ] Buy a car. ➕ {TODAY} ^t-" in after
    assert "Follow up with Alex about the Northwind requests" in after


def test_negation_never_passes_any_distance(vault, make_watcher):
    async def negated(text, today):
        return [{"text": "Don't buy a car", "due": None, "priority": None, "owed_by": None}]

    w = make_watcher(vault, capture_extractor=negated, disabled=FOCUS, n_max=20)
    inbox = vault / "Tasks/Inbox.md"
    inbox.write_text(inbox.read_text().rstrip("\n") + "\nbuy a car\n")
    summary = run(w)
    assert summary["auto"] == 0  # ~92% similar, 100% wrong — checkbox required
    (p,) = proposals.pending(w.proposals_dir)
    assert p["kind"] == "inbox_extract"


def test_word_mashup_fails_sequence_coverage(vault, make_watcher):
    async def mashup(text, today):
        return [{"text": "detections buy the car test", "due": None,
                 "priority": None, "owed_by": None}]

    w = make_watcher(vault, capture_extractor=mashup, disabled=FOCUS, n_max=20)
    inbox = vault / "Tasks/Inbox.md"
    inbox.write_text(inbox.read_text().rstrip("\n") + "\nbuy a car\ntest the detections\n")
    summary = run(w)
    assert summary["auto"] == 0  # every word is the user's; the sentence is not


def test_strict_fidelity_queues_grammar_edits(vault, make_watcher):
    (vault / "Harness").mkdir(exist_ok=True)
    (vault / "Harness/Spec.md").write_text("---\nfidelity: strict\n---\n\n# Vault Spec\n")

    async def polished(text, today):
        return [{"text": "Follow up with Alex about the Northwind requests",
                 "due": None, "priority": None, "owed_by": None}]

    w = make_watcher(vault, capture_extractor=polished, disabled=FOCUS, n_max=20)
    inbox = vault / "Tasks/Inbox.md"
    inbox.write_text(inbox.read_text().rstrip("\n")
                     + "\nfollow up with alex about northwind requests\n")
    summary = run(w)
    assert summary["auto"] == 0  # "the" is invented under strict
    (p,) = proposals.pending(w.proposals_dir)
    assert p["kind"] == "inbox_extract"


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
                     disabled=("capture", "inbox_extract", "spec_drift", "grouping", "archive_done"))
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
                     disabled=("capture", "inbox_extract", "spec_drift", "grouping", "archive_done"))
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
                     disabled=("capture", "inbox_extract", "spec_drift", "grouping", "archive_done"))
    summary = run(w)
    auto_syncs = [r for r in proposals.load_all(w.proposals_dir)
                  if r["status"] == "auto" and r["drafted_against"]["note"] == "Meetings/Sync.md"]
    assert auto_syncs == [] and summary["auto"] == 0


def test_faithful_capture_lands_and_clears_without_checkbox(vault, make_watcher):
    w = make_watcher(vault, capture_extractor=faithful, disabled=FOCUS, n_max=20)
    run(w)  # creates the capture surface
    surface = vault / "Harness/Capture.md"
    surface.write_text(surface.read_text() + "\nbuy a car\n")
    summary = run(w)
    assert summary["auto"] == 1
    assert "buy a car" in (vault / "Tasks/Inbox.md").read_text()
    assert surface.read_text() == CAPTURE_SKELETON  # cleared by the on_applied hook
    assert proposals.pending(w.proposals_dir) == []


def test_schema_fix_aimed_at_another_note_is_never_a_proposal(vault, make_watcher):
    """The path pin: the worker authors full tool args, and a schema fix
    has no legitimate cross-note step — so a plan aimed anywhere but the
    drafted note is a malfunction that dies at draft time. It must not
    even queue: an inattentive approve would still execute it."""
    note = vault / "Meetings/Sync.md"
    note.write_text(MEETING_NO_ID)
    inbox_before = (vault / "Tasks/Inbox.md").read_text()

    async def hijacking_drafter(item):
        return {"title": "stamp", "summary": "id only",
                "apply": [
                    {"tool": "update_frontmatter",
                     "args": {"path": item["note"], "key": "id", "value": item["new_id"]}},
                    # simulated against Sync.md this is an innocent added
                    # heading; applied for real it writes to the inbox
                    {"tool": "append_to_note",
                     "args": {"path": "Tasks/Inbox.md", "content": "# injected"}},
                ]}

    w = make_watcher(vault, drafter=hijacking_drafter, n_max=20,
                     disabled=("capture", "inbox_extract", "spec_drift", "grouping", "archive_done"))
    summary = run(w)
    assert summary["auto"] == 0
    assert (vault / "Tasks/Inbox.md").read_text() == inbox_before
    assert "id: m-" not in note.read_text()
    # no proposal for Sync.md at all — the cross-note plan died at draft
    # time (the fixture Inbox draws its own, legitimately same-note, draft)
    assert [p for p in proposals.pending(w.proposals_dir)
            if p["drafted_against"]["note"] == "Meetings/Sync.md"] == []


def test_verify_pins_the_target_note_as_defense_in_depth():
    """Layer two of the path pin: even if a cross-note plan reached the
    auto-apply gate some other way, the verifier refuses to vouch."""
    from harness.processors import verify_schema_fix

    item = {"note": "Meetings/Sync.md", "content": MEETING_NO_ID}
    plan = {"apply": [{"tool": "append_to_note",
                       "args": {"path": "Tasks/Inbox.md", "content": "# x"}}]}
    failures = verify_schema_fix(None, item, plan)
    assert failures and "targets" in failures[0]


def test_dropped_worker_date_forces_the_checkbox(vault, make_watcher):
    """Faithful text but a lost date: the fidelity verifier can't see
    structured fields, so a sanitized-away impossible date must force
    the checkbox and say so in the issues list."""
    async def date_dropper(text, today):
        return [{"text": "buy a car", "due": None, "priority": None,
                 "owed_by": None, "dropped_due": "2026-13-01"}]

    w = make_watcher(vault, capture_extractor=date_dropper, disabled=FOCUS, n_max=20)
    inbox = vault / "Tasks/Inbox.md"
    inbox.write_text(inbox.read_text().rstrip("\n") + "\nbuy a car\n")
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
                     disabled=("capture", "inbox_extract", "spec_drift", "grouping", "archive_done"))
    summary = run(w)
    assert summary["auto"] >= 1
    assert "id: m-" in note.read_text()


def test_stale_extraction_re_arms_instead_of_vanishing(vault, make_watcher):
    """The capture silent-drop bug, generalized (on_stale): a pending
    extraction that goes stale from note churn must clear its seen-hash,
    or the unchanged prose would never re-draft and the extraction would
    silently vanish."""
    (vault / "Harness").mkdir(exist_ok=True)
    (vault / "Harness/Spec.md").write_text("---\nauto_apply: []\n---\n\n# Vault Spec\n")
    w = make_watcher(vault, capture_extractor=faithful, disabled=FOCUS, n_max=20)
    inbox = vault / "Tasks/Inbox.md"
    inbox.write_text(inbox.read_text().rstrip("\n") + "\nbuy a car\n")
    run(w)
    (p1,) = proposals.pending(w.proposals_dir)
    # whitespace-only churn (autosave artifact): voids the pending draft
    inbox.write_text(inbox.read_text() + "\n\n")
    summary = run(w)
    assert summary["stale"] >= 1
    (p2,) = proposals.pending(w.proposals_dir)
    assert p2["kind"] == "inbox_extract" and p2["id"] != p1["id"]  # re-drafted, not dropped
