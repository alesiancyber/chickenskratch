"""The audit view — "what changed while I wasn't looking".

Verified auto-apply means the harness edits the vault without asking, so
this exists to answer for it. A rendered view of the vault's own git log:
every row is one snapshot commit, so revert (the dashboard's ↺, or
`tezcat-revert`) always applies. Rendered by `tezcat changes` and
/api/changes — never written into the vault, and deliberately never a
revert checkbox: destructive actions don't ride text files.
"""

from __future__ import annotations

from tezcat_vault.vault import Vault

HEADER = """\
# Changes

_Recent vault snapshots, newest first — everything the harness (or you,
between passes) committed. Every row is one git commit; undo from the
dashboard, or `tezcat-revert <sha>` in a terminal. Rows made inside a
chat turn carry its chat id._
"""


# Retired machine surfaces. The engine no longer writes them, but their
# render commits are still in existing vaults' git history, and an
# "update Harness/Today.md" row is never news. Dropping these from the
# filter would flood the Changes list with the harness's old redraws.
LEGACY_RENDERS = ("Harness/Review.md", "Harness/Today.md", "Harness/Changes.md")


def machine_rels(cfg, *extra: str) -> set[str]:
    """Surface paths whose commits are renders, not news — the input to
    is_machine_render, derived from the vault spec in one place."""
    return ({r for r in cfg.surfaces.values() if r}
            | set(LEGACY_RENDERS) | {e for e in extra if e})


def is_machine_render(commit: dict, machine: set[str]) -> bool:
    """Did this commit touch nothing but machine-rendered surfaces? Such a
    commit is the harness redrawing its own surfaces, so it is never news:
    listing it would let one render feed the next. A commit with no file
    list (a merge) is real history and never machine."""
    return bool(commit["files"]) and all(f in machine for f in commit["files"])


def recent_news(v: Vault, machine: set[str], limit: int = 20) -> list[dict]:
    """The commits worth showing: newest-first snapshots minus the
    harness redrawing its own surfaces. Both renderers of this list — the
    Changes.md surface and the dashboard's JSON — had written the same
    fetch-filter-cap loop; it lives here once, so the two can never drift
    on what counts as news."""
    rows = []
    for c in v.history(limit + 10):  # headroom for filtered rows
        if is_machine_render(c, machine):
            continue
        rows.append(c)
        if len(rows) >= limit:
            break
    return rows


def render(v: Vault, cfg, limit: int = 20) -> str:
    """Deterministic markdown: absolute times (relative ones would make
    two renders a minute apart disagree), and machine-render commits —
    any commit touching only harness surfaces, present or retired —
    filtered out, so the harness redrawing itself is never news."""
    lines = []
    for c in recent_news(v, machine_rels(cfg), limit):
        when = c["date"][:16].replace("T", " ")
        files = ", ".join(f"`{f}`" for f in c["files"][:3])
        more = f" +{len(c['files']) - 3} more" if len(c["files"]) > 3 else ""
        chat = f" · chat `{c['chat_id'][:12]}`" if c["chat_id"] else ""
        lines.append(f"- **{when}** {c['subject']} — {files}{more}{chat}")
    return HEADER + "\n" + "\n".join(lines) + "\n"
