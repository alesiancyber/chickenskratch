"""GitHub-style split diffs: the diffhtml renderer and its Review.md
integration (diff_style: split, the default)."""

import asyncio

import pytest

from harness import diffhtml, proposals


def test_split_diff_marks_rows_and_words():
    before = "# Note\n\nkeep this line\nthe quick brown fox\nremove me\n"
    after = "# Note\n\nkeep this line\nthe quick red fox\nnew line here\n"
    out = diffhtml.split_diff(before, after)
    assert out.startswith("<table") and out.endswith("</table>")
    assert "\n\n" not in out  # a blank line would end the markdown HTML block
    assert "keep this line" in out
    # word-level: only the changed word is span-wrapped
    assert ">brown</span>" in out and ">red</span>" in out
    # paired change rows keep both sides' words (marks may fragment them)
    for word in ("remove", "me", "new", "here"):
        assert word in out


def test_split_diff_escapes_html_and_collapses_long_runs():
    before = "<script>alert(1)</script>\n" + "\n".join(f"same {i}" for i in range(30)) + "\nend"
    after = "<b>changed</b>\n" + "\n".join(f"same {i}" for i in range(30)) + "\nend CHANGED"
    out = diffhtml.split_diff(before, after)
    assert "<script>" not in out and "&lt;script&gt;" in out  # escaped, never live
    assert "unchanged line(s)" in out  # the long identical run collapsed


def test_identical_texts_render_nothing():
    assert diffhtml.split_diff("same\n", "same\n") == ""


@pytest.fixture()
def vault(gitvault):
    return gitvault


def test_review_renders_split_table_by_default(vault, make_watcher):
    async def rewording_drafter(item):
        # touches user text -> verifier queues it -> it renders with a diff
        return {"title": f"Rework {item['note']}", "summary": "reword",
                "apply": [{"tool": "update_frontmatter",
                           "args": {"path": item["note"], "key": "status", "value": "rewritten"}}]}

    w = make_watcher(vault, drafter=rewording_drafter, n_max=1,
                     disabled=("capture", "inbox_extract", "spec_drift", "grouping"))
    asyncio.run(w.run_once())
    assert len(proposals.pending(w.proposals_dir)) >= 1
    review = (vault / "Harness/Review.md").read_text()
    assert "<table" in review  # side-by-side HTML, not a ```diff fence
    assert "```diff" not in review
    assert "- [ ] approve p-" in review  # checkboxes still plain markdown
