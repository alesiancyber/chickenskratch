"""The structured diff view-model — the promoted computation the
dashboard renders as rows, and `applier.preview` renders as a unified
```diff for anything text-shaped (the CLI, a proposal's own record).

One computation, two renderers — neither renderer owns diff logic of
its own."""

import asyncio

import pytest

from chickenskratch import diffs, proposals


def test_diff_rows_marks_rows_and_words():
    before = "# Note\n\nkeep this line\nthe quick brown fox\nremove me\n"
    after = "# Note\n\nkeep this line\nthe quick red fox\nnew line here\n"
    rows = diffs.diff_rows(before, after)
    kinds = [r["kind"] for r in rows]
    assert "context" in kinds and "change" in kinds
    # word-level: only the changed word is marked on the paired line
    pair = next(r for r in rows if r["kind"] == "change" and r["left"] and r["right"])
    assert [s["text"] for s in pair["left"] if s["mark"]] == ["brown"]
    assert [s["text"] for s in pair["right"] if s["mark"]] == ["red"]
    # unpaired sides carry None line numbers, unmarked segments
    for r in rows:
        if r["kind"] == "change":
            assert (r["ln"] is None) == (r["left"] is None)
            assert (r["rn"] is None) == (r["right"] is None)


def test_diff_rows_collapses_long_equal_runs():
    before = "start\n" + "\n".join(f"same {i}" for i in range(30)) + "\nend"
    after = "START\n" + "\n".join(f"same {i}" for i in range(30)) + "\nend CHANGED"
    rows = diffs.diff_rows(before, after)
    (skip,) = [r for r in rows if r["kind"] == "skip"]
    context = [r for r in rows if r["kind"] == "context"]
    # 3 lines of context hug each change; the middle collapses
    assert skip["count"] == 30 - len(context)
    assert skip["count"] > 0


def test_identical_texts_diff_to_nothing():
    assert diffs.diff_rows("same\n", "same\n") == []


@pytest.fixture()
def vault(gitvault):
    return gitvault


def test_a_queued_proposal_previews_as_a_unified_diff(vault, make_watcher):
    """One drafted proposal, two renderings of the same simulate: the
    dashboard's rows and the plain unified fence. Neither is HTML — the
    inline-styled emitter was cut in Phase 0 and must not come back."""
    from chickenskratch import applier

    async def rewording_drafter(item):
        return {"title": f"Rework {item['note']}", "summary": "reword",
                "apply": [{"tool": "update_frontmatter",
                           "args": {"path": item["note"], "key": "status", "value": "rewritten"}}]}

    w = make_watcher(vault, drafter=rewording_drafter, n_max=1,
                     disabled=("capture", "spec_drift", "grouping"))
    asyncio.run(w.run_once())
    (p, *_) = proposals.pending(w.proposals_dir)

    note_text = (vault / p["drafted_against"]["note"]).read_text()
    fence = applier.preview(note_text, p)
    assert "+status: rewritten" in fence
    assert "<table" not in fence         # the HTML emitter stays gone

    after, _skipped = applier.simulate_for_note(note_text, p)
    rows = diffs.diff_rows(note_text, after)
    assert {r["kind"] for r in rows} <= {"context", "change", "skip"}
    assert any(r["kind"] == "change" for r in rows)


def test_a_plan_that_cannot_be_simulated_is_described_not_raised():
    """Simulation is best-effort by definition. A worker plan carrying
    malformed YAML used to throw yaml.ScannerError straight out — into
    the dashboard's /api/review (a 500 that empties the queue view) and,
    once the verifier shared this walk, into the auto-apply gate."""
    from chickenskratch import applier

    p = {"drafted_against": {"note": "Home.md"}, "apply": [
        {"tool": "add_task", "args": {"path": "Home.md", "text": "x"}},
        {"tool": "update_frontmatter",
         "args": {"path": "Home.md", "key": "k", "value": "a: b: c"}},
    ]}
    after, skipped = applier.simulate_for_note("# Home\n", p)
    assert after == "# Home\n" and len(skipped) == 2
    assert any("ScannerError" in s for s in skipped)
    # and the strict reading refuses to vouch, rather than exploding
    assert applier.simulate("# Home\n", p) is None
    # the Review.md preview still renders something honest
    assert "not previewed" in applier.preview("# Home\n", p)
