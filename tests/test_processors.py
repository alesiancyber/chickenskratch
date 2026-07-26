"""Phase 7: the engine is generic. Processors
are registry entries, priority is registry order, rollback is a name in
`disabled`, and concurrent passes are serialized by the pass lock."""

import asyncio
import fcntl

import pytest

from harness import applier, processors, proposals
from harness.processors import ProcessorSpec
from obsidian_mcp.vault import Vault


@pytest.fixture()
def vault(gitvault):
    # unified diffs + no auto: this suite asserts the queue path literally
    (gitvault / "Harness").mkdir(exist_ok=True)
    (gitvault / "Harness/Spec.md").write_text('---\nauto_apply: []\n---\n\n# Vault Spec\n')
    return gitvault


def run(w):
    return asyncio.run(w.run_once())


def test_default_registry_order_is_priority_order():
    # capture first: active user input must never be starved out of n_max
    # (//commands are a pass phase before drafting, not a processor)
    assert [p.name for p in processors.REGISTRY] == [
        "capture", "inbox_extract", "archive_done", "spec_drift", "schema_fix", "grouping"]


def test_disable_is_per_processor_and_unknown_names_fail_loud(vault, make_watcher):
    w = make_watcher(
        vault, disabled=("schema_fix", "grouping", "spec_drift", "inbox_extract", "archive_done"))
    assert [p.name for p in w.processors] == ["capture"]
    with pytest.raises(ValueError, match="groupnig"):
        processors.registry(disabled=("groupnig",))


def test_fourth_processor_runs_end_to_end_without_engine_changes(vault, make_watcher):
    """A processor the engine has never heard of: scanned, drafted,
    queued, approved, applied, hook fired."""
    applied_hook = []

    def scan(w):
        sha, _ = applier.vault_state(w.vault_path)
        v = Vault(w.vault_path)
        note = "Daily/2026-07-16.md"
        return [{"kind": "stamp_footer", "note": note, "vault_sha": sha,
                 "content": v.read(note)}]

    async def draft(w, item):
        return proposals.new_proposal(
            kind="stamp_footer",
            note=item["note"],
            vault_sha=item["vault_sha"],
            note_content=item["content"],
            title=f"Stamp footer on {item['note']}",
            summary="adds a footer line",
            issues=["missing footer"],
            apply=[{"tool": "append_to_note",
                    "args": {"path": item["note"], "content": "reviewed ✓"}}],
        )

    spec = ProcessorSpec(
        "stamp_footer", scan, draft,
        on_applied=lambda w, p: applied_hook.append(p["kind"]),
    )
    w = make_watcher(vault, registry=(spec,))
    run(w)
    (p,) = proposals.pending(w.proposals_dir)
    assert p["kind"] == "stamp_footer"
    assert p["title"] == "Stamp footer on Daily/2026-07-16.md"

    summary = asyncio.run(w.run_once(decisions=[(p["id"], "approve")]))
    assert summary["approved"] == 1
    assert "reviewed ✓" in (vault / "Daily/2026-07-16.md").read_text()
    assert applied_hook == ["stamp_footer"]


def test_concurrent_pass_is_skipped_by_the_lock(vault, make_watcher):
    w = make_watcher(vault, registry=())
    lock_path = w.state_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        summary = run(w)
        assert summary["locked"] is True
        assert summary["scanned"] is False and summary["drafted"] == 0
    finally:
        holder.close()
    summary = run(w)
    assert summary["locked"] is False  # lock released -> pass runs


def test_archive_done_end_to_end(vault, make_watcher):
    """Done tasks in the working inboxes get filed into the archive —
    deterministic, verified, auto-applied without a checkbox."""
    inbox = vault / "Tasks/Inbox.md"
    inbox.write_text(inbox.read_text().rstrip("\n")
                     + "\n- [x] shipped the thing ✅ 2026-07-20 ^t-ddddddddddd1\n")
    (vault / "Harness").mkdir(exist_ok=True)
    (vault / "Harness/Spec.md").write_text(
        "---\nauto_apply: [archive_done]\n---\n\n# Vault Spec\n")
    w = make_watcher(vault, registry=(processors.ARCHIVE_DONE,))
    summary = run(w)
    assert summary["auto"] == 1
    assert "[x]" not in inbox.read_text()
    archive = (vault / "Tasks/Archive.md").read_text()
    assert "- [x] shipped the thing ✅ 2026-07-20 ^t-ddddddddddd1" in archive
    # clean next pass: nothing to re-archive
    (vault / "Home.md").write_text("nudge\n")
    assert run(w)["auto"] == 0


def test_one_mis_encoded_note_does_not_disable_schema_fixing(vault, make_watcher):
    """validate_schema walks with read_lossy, so it reports notes the
    strict read then rejects — and the engine catches scan failures per
    PROCESSOR, so one latin-1 note used to silently kill schema fixing
    entirely, every pass. The bad note loses its fix; the others keep
    theirs."""
    from harness import applier
    (vault / "Meetings").mkdir(exist_ok=True)
    (vault / "Meetings" / "2026-07-01 Bad.md").write_bytes(
        "---\ntype: meeting\n---\n# Caf\xe9 sync\n".encode("latin-1"))
    (vault / "Meetings" / "2026-07-02 Good.md").write_text(
        "---\ntype: meeting\n---\n# Sync\n")
    applier.use_vault(str(vault))
    w = make_watcher(vault)
    notes = [i["note"] for i in processors.scan_schema(w)]
    assert "Meetings/2026-07-02 Good.md" in notes
    assert "Meetings/2026-07-01 Bad.md" not in notes


def test_inbox_scan_survives_a_note_that_opens_with_a_horizontal_rule(vault, make_watcher):
    """The inbox scan used to hand-roll frontmatter stripping by splitting
    on "---", so a note whose FIRST line is a horizontal rule (no
    frontmatter at all) lost its opening line before the section walk."""
    from harness import applier
    inbox = vault / "Tasks" / "Inbox.md"
    inbox.write_text("---\n\n## Notes\n\ncall the bank about the wire\n")
    applier.use_vault(str(vault))
    w = make_watcher(vault)
    (item,) = processors.scan_inbox(w)
    assert item["prose"] == ["call the bank about the wire"]
    assert item["section"] == "Notes"
