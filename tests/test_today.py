"""The attention view. A read-only render: no trust machinery,
deterministic within a day, one screen. It stopped being written into the
vault on 2026-07-26 — it is what `harness today` prints and what the
dashboard's /api/today collects — so these tests render it directly
instead of reading a note back."""

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


def view(vault, pending=0):
    """The Today view as text — the CLI's exact render path."""
    from harness import today as today_mod
    from obsidian_mcp.vault import Vault
    from obsidian_mcp.vaultconfig import get_config

    cfg = get_config(str(vault))
    return today_mod.render(Vault(str(vault), exclude=cfg.exclude), cfg, pending)


def test_today_renders_the_attention_surface(vault, make_watcher):
    w = make_watcher(vault, registry=())
    run(w)
    text = view(vault)
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
    assert "1 pending decision(s)" in view(vault, pending=1)


def test_today_is_deterministic_and_writes_nothing(vault, make_watcher):
    """Two passes with an unchanged vault: byte-identical view, and the
    pass leaves no note behind. Rendering this used to BE a vault write —
    a commit whose only purpose was to redraw a view of itself."""
    w = make_watcher(vault, registry=())
    run(w)
    text = view(vault)
    run(w)
    assert view(vault) == text
    assert not (vault / "Harness/Today.md").exists()


def test_waiting_age_and_stranded_meeting_actions(vault, make_watcher):
    """Views powered by lifecycle metadata: ➕ ages on waiting items, and
    open UNDATED action items rotting in old meeting notes."""
    (vault / "Tasks/Waiting.md").write_text(
        f"# Waiting\n\n- [ ] Marcus: firewall logs ➕ {day(-12)}\n")
    (vault / "Meetings" / f"{day(-10)} Vendor Sync.md").write_text(
        "# Vendor Sync\n\n## Action items\n\n- [ ] chase the SOW redlines\n")
    w = make_watcher(vault, registry=())
    run(w)
    text = view(vault)
    assert "Marcus: firewall logs (12d waiting)" in text
    assert "## Stranded in meetings" in text
    assert "chase the SOW redlines — 10d old" in text
    # dated meeting items are Due-section business, not stranded
    assert text.count("chase the SOW redlines") == 1


def test_impossible_dates_report_instead_of_crashing_the_pass(vault, make_watcher):
    """A hand-typed 📅 2026-02-30 has the right shape but is not a date.
    The dashboard must flag it — and the engine pass must survive it."""
    (vault / "Tasks/Sprint.md").write_text(
        "# Sprint\n\n- [ ] typo'd deadline 📅 2026-02-30\n")
    (vault / "Tasks/Waiting.md").write_text(
        "# Waiting\n\n- [ ] Marcus: firewall logs ➕ 2026-13-01\n")
    w = make_watcher(vault, registry=())
    run(w)  # a ValueError here used to kill every pass until the note was fixed
    text = view(vault)
    assert "typo'd deadline" in text and "not a real date" in text
    assert "Marcus: firewall logs" in text  # row survives, bogus age suppressed
    assert "waiting)" not in text
