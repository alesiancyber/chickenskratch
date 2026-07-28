"""The attention view. A read-only render: no trust machinery,
deterministic within a day, one screen. It stopped being written into the
vault on 2026-07-26 — it is what `tezcat today` prints and what the
dashboard's /api/today collects — so these tests render it directly
instead of reading a note back."""

import asyncio
import datetime as dt

import pytest

from tezcat import proposals

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
    from tezcat import today as today_mod
    from tezcat_vault.vault import Vault
    from tezcat_vault.vaultconfig import get_config

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


def test_planned_meeting_actions_are_dispositioned_not_stranded(vault, make_watcher):
    """Promote-to-today sets ⏳ — that IS the disposition a stranded row
    asks for, so a planned item must leave the stranded list instead of
    rowing there and in the time model at once."""
    (vault / "Meetings" / f"{day(-10)} Vendor Sync.md").write_text(
        "# Vendor Sync\n\n## Action items\n\n"
        f"- [ ] chase the SOW redlines ⏳ {day(0)}\n")
    w = make_watcher(vault, registry=())
    run(w)
    text = view(vault)
    assert "chase the SOW redlines — 10d old" not in text  # not stranded
    assert text.count("chase the SOW redlines") == 1       # planned-today's row now


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


def test_week_buckets_are_calendar_weeks(vault):
    """The this-week / next-week model: hard dues and ⏳ plans file into
    calendar weeks (Sunday-anchored), each row labeled with the field
    that placed it; anything dated past next week is `later`, and the
    undated-unplanned stay the backlog's business."""
    import datetime as _dt

    from tezcat import today as today_mod
    from tezcat_vault.vault import Vault
    from tezcat_vault.vaultconfig import get_config

    this_sunday = today_mod.week_end(TODAY)
    next_sunday = this_sunday + _dt.timedelta(days=7)
    (vault / "Tasks/Sprint.md").write_text(
        "# Sprint\n\n"
        f"- [ ] planned for this week ⏳ {this_sunday}\n"
        f"- [ ] lined up for next week ⏳ {next_sunday}\n"
        f"- [ ] hard due next week 📅 {next_sunday}\n"
        f"- [ ] renew the domain 📅 {day(30)}\n"
    )
    cfg = get_config(str(vault))
    model = today_mod.collect(Vault(str(vault), exclude=cfg.exclude), cfg, 0)
    assert model["due"]["this_sunday"] == this_sunday.isoformat()

    def texts(bucket):
        return [r["text"] for r in model[bucket]]

    if this_sunday != TODAY:  # on a Sunday "this week" collapses into today
        assert "planned for this week" in texts("this_week")
    kinds = {r["text"]: r["kind"] for r in model["next_week"]}
    assert kinds.get("lined up for next week") == "planned"
    assert kinds.get("hard due next week") == "due"
    assert "renew the domain" in texts("later")
    assert "renew the domain" not in texts("this_week") + texts("next_week")


def test_capture_with_week_language_lands_scheduled_not_due(vault, make_watcher):
    """Typing "this week" plans the task (⏳ Sunday anchor); it must
    never fabricate a 📅 deadline."""
    from tezcat import processors as procs
    from tezcat import today as today_mod

    sunday = today_mod.week_end(TODAY).isoformat()

    async def extractor(text, today):
        return [{"text": "sort out the vendor contract", "due": None,
                 "scheduled": sunday, "priority": None, "owed_by": None}]

    (vault / "Harness/Capture.md").write_text("sort out the vendor contract this week\n")
    w = make_watcher(vault, registry=(procs.CAPTURE,), capture_extractor=extractor)
    run(w)
    (p,) = proposals.pending(w.proposals_dir)
    assert f"planned for week of {sunday}" in p["issues"][0]
    assert p["apply"][0]["args"]["scheduled"] == sunday
    asyncio.run(w.run_once(decisions=[(p["id"], "approve")], lock_wait=5))
    line = next(ln for ln in (vault / "Tasks/Inbox.md").read_text().splitlines()
                if "vendor contract" in ln)
    assert f"⏳ {sunday}" in line and "📅" not in line
