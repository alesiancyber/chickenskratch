"""Harness/Today.md — the attention surface. A read-only view: no trust
machinery, deterministic within a day, one screen, and its own render
commit must never change what it renders (no self-feeding)."""

import asyncio
import datetime as dt

import pytest

from harness import proposals

TODAY = dt.date.today()


def day(n: int) -> str:
    return (TODAY + dt.timedelta(days=n)).isoformat()


@pytest.fixture()
def vault(gitvault):
    (gitvault / "Harness").mkdir(exist_ok=True)
    (gitvault / "Harness/Spec.md").write_text("---\nauto_apply: []\n---\n\n# Vault Spec\n")
    # plant a deterministic day: overdue, due today, due soon, out of horizon
    (gitvault / "Tasks/Sprint.md").write_text(
        "# Sprint\n\n"
        f"- [ ] rotate the leaked key 📅 {day(-2)}\n"
        f"- [ ] ship the detection pack 📅 {day(0)}\n"
        f"- [ ] draft the retro 📅 {day(2)}\n"
        f"- [ ] renew the domain 📅 {day(30)}\n"
        f"- [x] already done 📅 {day(-5)}\n"
    )
    (gitvault / "Tasks/Waiting.md").write_text(
        "# Waiting\n\n- [ ] Marcus: firewall logs\n")
    (gitvault / "Meetings" / f"{day(0)} Standup.md").write_text("# Standup\n")
    return gitvault


def run(w):
    return asyncio.run(w.run_once())


def test_today_renders_the_attention_surface(vault, make_watcher):
    w = make_watcher(vault, registry=())
    run(w)
    text = (vault / "Harness/Today.md").read_text()
    assert f"# Today — {TODAY.strftime('%A %Y-%m-%d')}" in text
    # due section: overdue marked, today marked, soon dated, horizon excluded
    assert "rotate the leaked key — ⚠ **2d overdue**" in text
    assert "ship the detection pack — **due today**" in text
    assert "draft the retro — due in 2d" in text
    assert "renew the domain" not in text  # beyond the 3-day horizon
    assert "already done" not in text      # completed tasks are not attention
    # ordering: overdue before today before soon
    assert text.index("rotate the leaked key") < text.index("ship the detection pack") \
        < text.index("draft the retro")
    assert "## Waiting on" in text and "Marcus: firewall logs" in text
    assert "## Today's meetings" in text and f"{day(0)} Standup" in text
    # fixture board has a Doing column with a card
    assert "## In flight" in text and "Doing" in text
    # I4: Private/ content never surfaces — the fixture tripwire caught a
    # live leak on 2026-07-23 (the watcher's Vault lacked the exclusions)
    assert "PRIVATE-FIXTURE-TASK" not in text
    assert "Private/" not in text


def test_today_shows_pending_decision_count(vault, make_watcher):
    w = make_watcher(vault, registry=())
    run(w)
    p = proposals.new_proposal(
        kind="grouping", note="Tasks/Sprint.md", vault_sha="",
        note_content=(vault / "Tasks/Sprint.md").read_text(),  # fresh: survives auto-stale
        title="t", summary="s", issues=[],
        apply=[{"tool": "replace_section",
                "args": {"path": "Tasks/Sprint.md", "heading": "Sprint", "content": "y"}}])
    proposals.save(p, w.proposals_dir)
    (vault / "Home.md").write_text("nudge\n")
    run(w)
    assert "1 pending decision(s)" in (vault / "Harness/Today.md").read_text()


def test_today_is_stable_and_never_feeds_itself(vault, make_watcher):
    """Two passes with an unchanged vault: byte-identical surface, no
    re-write — the render's own commit must not change the activity
    count (Harness/-only commits are excluded from it)."""
    w = make_watcher(vault, registry=())
    run(w)
    surface = vault / "Harness/Today.md"
    text = surface.read_text()
    stamp = surface.stat().st_mtime
    run(w)
    assert surface.read_text() == text
    assert surface.stat().st_mtime == stamp
