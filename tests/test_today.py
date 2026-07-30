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
    (gitvault / "Harness/Spec.md").write_text(
        "---\npolicies: {capture: ask}\n---\n\n# Vault Spec\n")
    # plant a deterministic day: overdue, due today, due soon, out of horizon
    (gitvault / "Tasks/Sprint.md").write_text(
        "# Sprint\n\n"
        f"- [ ] rotate the leaked key 📅 {day(-2)}\n"
        f"- [ ] ship the detection pack 📅 {day(0)}\n"
        f"- [ ] draft the retro 📅 {day(2)}\n"
        f"- [ ] renew the domain 📅 {day(30)}\n"
        f"- [x] already done 📅 {day(-5)}\n"
    )
    (gitvault / "Meetings" / f"{day(-12)} SOC Sync.md").write_text(
        f"# SOC Sync\n\n## Waiting on\n\n"
        f"- [ ] firewall logs 👤 Marcus ➕ {day(-12)}\n")
    (gitvault / "Meetings" / f"{day(0)} Standup.md").write_text("# Standup\n")
    return gitvault


def run(w):
    return asyncio.run(w.run_once())


def _vault_and_cfg(vault):
    from tezcat_vault.vault import Vault
    from tezcat_vault.vaultconfig import get_config

    cfg = get_config(str(vault))
    return Vault(str(vault), exclude=cfg.exclude), cfg


def view(vault, pending=0):
    """The Today view as text — the CLI's exact render path."""
    from tezcat import today as today_mod

    v, cfg = _vault_and_cfg(vault)
    return today_mod.render(v, cfg, pending)


def model(vault, pending=0):
    """The view's MODEL — what the dashboard collects, before the render
    folds each section behind a "+N more". Membership questions belong
    here: a row that fell behind the fold is absent from the text for a
    reason that has nothing to do with the rule under test."""
    from tezcat import today as today_mod

    v, cfg = _vault_and_cfg(vault)
    return today_mod.collect(v, cfg, pending)


def stranded_texts(vault):
    return [r["text"] for r in model(vault)["stranded"]]


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
    (vault / "Meetings" / f"{day(-10)} Vendor Sync.md").write_text(
        "# Vendor Sync\n\n## Action items\n\n- [ ] chase the SOW redlines\n")
    w = make_watcher(vault, registry=())
    run(w)
    text = view(vault)
    assert "Marcus: firewall logs (12d waiting)" in text
    assert "## Stranded — undated, aging" in text
    # the model, not the text: the section folds past a few rows, and the
    # fixture vault already fills it — a cap is not a judgment about
    # whether this item is stranded
    m = model(vault)
    rows = [r for r in m["stranded"] if r["text"] == "chase the SOW redlines"]
    assert len(rows) == 1 and rows[0]["age_days"] == 10
    # stranded is for UNdated items only — a dated one is Due's business,
    # and no item may row in both
    assert not any("chase the SOW redlines" in r["text"]
                   for bucket in m["due"].values() if isinstance(bucket, list)
                   for r in bucket)
    # oldest first: the fold shows the most-rotted, never an arbitrary few
    ages = [r["age_days"] for r in m["stranded"]]
    assert ages == sorted(ages, reverse=True)


def test_planned_meeting_actions_are_dispositioned_not_stranded(vault, make_watcher):
    """Promote-to-today sets ⏳ — that IS the disposition a stranded row
    asks for, so a planned item must leave the stranded list instead of
    rowing there and in the time model at once."""
    (vault / "Meetings" / f"{day(-10)} Vendor Sync.md").write_text(
        "# Vendor Sync\n\n## Action items\n\n"
        f"- [ ] chase the SOW redlines ⏳ {day(0)}\n")
    w = make_watcher(vault, registry=())
    run(w)
    # the model, not the text: a row that merely fell behind the section's
    # fold is also "not in text", so the render can pass this for a reason
    # that has nothing to do with the ⏳ rule
    assert "chase the SOW redlines" not in stranded_texts(vault)
    assert view(vault).count("chase the SOW redlines") == 1  # planned-today's row now


def test_owed_items_surface_in_waiting_never_as_own_work(vault):
    """The 👤 partition lives in the walk — the one choke point — so an
    owed item can never read as the user's work: not planned, not
    backlog, not stranded. Snoozing is its ⏳ ("chase this then"): a
    future ⏳ hides it from the waiting view until it arrives."""
    (vault / "Meetings" / f"{day(-10)} Azure Sync.md").write_text(
        "# Azure Sync\n\n## Waiting on\n\n"
        f"- [ ] pipeline costs 👤 Nadia ➕ {day(-10)} ^t-aaaaaaaaaaaa\n"
        f"- [ ] contract redlines 👤 Legal ⏳ {day(3)} ➕ {day(-10)} ^t-bbbbbbbbbbbb\n")
    m = model(vault)
    # owed and unsnoozed: the waiting view, with owner, age, and the
    # record the commitment was made in (the row links context back)
    (row,) = [r for r in m["waiting"] if r["text"] == "pipeline costs"]
    assert row["owner"] == "Nadia" and row["waiting_days"] == 10
    assert row["note"] == f"Meetings/{day(-10)} Azure Sync.md"
    # the snooze field rides on the row: the dashboard's plan chips
    # compute placement from it with the same rule as every task row
    assert row["scheduled"] == ""
    # snoozed (⏳ in the future): out of the waiting view until its day
    # comes, but NOT invisible — it files into the week lanes as a chase
    # row with its owner attached. (It used to be nowhere at all, so
    # snoozing an owed item read as the item being deleted.)
    assert not any(r["text"] == "contract redlines" for r in m["waiting"])
    lanes = m["this_week"] + m["next_week"] + m["later"]
    (chase,) = [r for r in lanes if r["text"] == "contract redlines"]
    assert chase["kind"] == "chase" and chase["owner"] == "Legal"
    assert chase["scheduled"] == day(3)
    # the partition guarantee, restated: owed items never appear as the
    # user's OWN work — no ownerless row anywhere carries them
    own_texts = [r["text"] for r in m["planned"] + m["backlog"] + m["stranded"]
                 + [r for r in lanes if not r.get("owner")]]
    assert "pipeline costs" not in own_texts
    assert "contract redlines" not in own_texts
    # and the markdown render names the owner
    assert "Nadia: pipeline costs" in view(vault)


def test_backlog_and_stranded_span_the_records(vault):
    """The inbox is retired: a record's action items are as plannable
    (backlog) and as prone to rot (stranded) as anything an inbox held.
    Age comes from ➕ created — the filename date is only the fallback
    clock for pre-stamp lines — so a task added to an old note ages
    from its own birth, not the note's."""
    (vault / "Meetings" / f"{day(-30)} Kickoff.md").write_text(
        "# Kickoff\n\n## Action items\n\n"
        f"- [ ] write the runbook ➕ {day(-20)} ^t-cccccccccccc\n"
        "- [ ] file the SOW\n")  # no ➕: the filename is its clock
    m = model(vault)
    (b,) = [r for r in m["backlog"] if r["text"] == "write the runbook"]
    assert b["age_days"] == 20 and b["note"] == f"Meetings/{day(-30)} Kickoff.md"
    (s,) = [r for r in m["stranded"] if r["text"] == "write the runbook"]
    assert s["age_days"] == 20                       # ➕ outranks the filename
    (s2,) = [r for r in m["stranded"] if r["text"] == "file the SOW"]
    assert s2["age_days"] == 30                      # fallback: filename date


def test_impossible_dates_report_instead_of_crashing_the_pass(vault, make_watcher):
    """A hand-typed 📅 2026-02-30 has the right shape but is not a date.
    The dashboard must flag it — and the engine pass must survive it."""
    (vault / "Tasks/Sprint.md").write_text(
        "# Sprint\n\n- [ ] typo'd deadline 📅 2026-02-30\n")
    (vault / "Meetings" / f"{day(-12)} SOC Sync.md").write_text(
        "# SOC Sync\n\n## Waiting on\n\n"
        "- [ ] firewall logs 👤 Marcus ➕ 2026-13-01\n")
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
    from tests.conftest import record_worker_returning
    from tezcat import processors as procs
    from tezcat import today as today_mod

    sunday = today_mod.week_end(TODAY).isoformat()
    worker = record_worker_returning(
        [{"text": "sort out the vendor contract", "due": None,
          "scheduled": sunday, "priority": None, "owed_by": None}])

    (vault / "Harness/Capture.md").write_text("sort out the vendor contract this week\n")
    w = make_watcher(vault, registry=(procs.CAPTURE,), record_worker=worker)
    run(w)
    (p,) = proposals.pending(w.proposals_dir)
    assert f"planned for week of {sunday}" in p["issues"][0]
    assert f"⏳ {sunday}" in p["apply"][0]["args"]["content"]
    asyncio.run(w.run_once(decisions=[(p["id"], "approve")], lock_wait=5))
    filed = (vault / "Captures" / f"{TODAY.isoformat()}.md").read_text()
    line = next(ln for ln in filed.splitlines()
                if "vendor contract" in ln and not ln.startswith(">"))
    assert f"⏳ {sunday}" in line and "📅" not in line
