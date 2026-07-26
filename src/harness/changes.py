"""Harness/Changes.md — the audit surface.

Verified auto-apply means the harness edits the vault without a
checkbox; this surface answers "what changed while I wasn't looking"
without leaving Obsidian. It is a rendered view of the vault's own git
log — every row is one snapshot commit, so revert (the dashboard's ↺,
or `harness-revert`) always applies. Deliberately no revert checkbox
(explicitly-cut list): destructive actions don't ride text files.
"""

from __future__ import annotations

from obsidian_mcp.vault import Vault

HEADER = """\
# Changes

_Recent vault snapshots, newest first — everything the harness (or you,
between passes) committed. Every row is one git commit; undo from the
dashboard, or `harness-revert <sha>` in a terminal. Rows made inside a
chat turn carry its chat id._
"""


def machine_rels(cfg, *extra: str) -> set[str]:
    """Surface paths whose commits are renders, not news — the input to
    is_machine_render, derived from the vault spec in one place."""
    return {r for r in cfg.surfaces.values() if r} | {e for e in extra if e}


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


def render(v: Vault, changes_rel: str, limit: int = 20, status: str = "",
           surface_rels: frozenset[str] | set[str] = frozenset()) -> str:
    """Deterministic markdown for the surface: absolute times (relative
    ones would churn the note every pass), and machine-render commits —
    any commit touching only harness surfaces — filtered out, so no
    surface's render can ever feed this one (Today.md renders after this
    surface does; listing it would churn a rewrite every pass). `status`
    is an optional health line (worker model + reachability) — stable
    across passes unless something actually changed."""
    lines = []
    for c in recent_news(v, set(surface_rels) | {changes_rel}, limit):
        when = c["date"][:16].replace("T", " ")
        files = ", ".join(f"`{f}`" for f in c["files"][:3])
        more = f" +{len(c['files']) - 3} more" if len(c["files"]) > 3 else ""
        chat = f" · chat `{c['chat_id'][:12]}`" if c["chat_id"] else ""
        lines.append(f"- **{when}** {c['subject']} — {files}{more}{chat}")
    head = HEADER + (f"\n{status}\n" if status else "")
    return head + "\n" + "\n".join(lines) + "\n"
