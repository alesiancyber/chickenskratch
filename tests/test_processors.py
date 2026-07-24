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
    (gitvault / "Harness/Spec.md").write_text('---\nauto_apply: []\ndiff_style: unified\n---\n\n# Vault Spec\n')
    return gitvault


def run(w):
    return asyncio.run(w.run_once())


def test_default_registry_order_is_priority_order():
    # capture first: active user input must never be starved out of n_max
    # (//commands are a pass phase before drafting, not a processor)
    assert [p.name for p in processors.REGISTRY] == [
        "capture", "inbox_extract", "spec_drift", "schema_fix", "grouping"]


def test_disable_is_per_processor_and_unknown_names_fail_loud(vault, make_watcher):
    w = make_watcher(
        vault, disabled=("schema_fix", "grouping", "spec_drift", "inbox_extract"))
    assert [p.name for p in w.processors] == ["capture"]
    with pytest.raises(ValueError, match="groupnig"):
        processors.registry(disabled=("groupnig",))


def test_fourth_processor_runs_end_to_end_without_engine_changes(vault, make_watcher):
    """A processor the engine has never heard of: scanned, drafted,
    rendered into Review.md, checkbox-approved, applied, hook fired."""
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
    review = vault / "Harness/Review.md"
    assert p["title"] in review.read_text()
    assert "```diff" in review.read_text()  # render_diff defaults on

    review.write_text(review.read_text().replace(
        f"- [ ] approve {p['id']}", f"- [x] approve {p['id']}"))
    summary = run(w)
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
