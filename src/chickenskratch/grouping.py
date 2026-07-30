"""Grouping G1: organize one flat task list.

Candidate sets are built in code, from entity boundaries, before any
model runs — semantic similarity alone can't know two clients' tasks
must never mix, but structure can. v1 scope is deliberately narrow: one
note, one section, tasks only, fully stamped. The drafter only ever
chooses a partition; membership integrity is enforced here, and the
apply plan rewrites the section with the original task lines
byte-identical.
"""

from __future__ import annotations

from tezcat_vault import markdown as md
from tezcat_vault import tasks as tk

# A list shorter than this isn't worth a proposal.
MIN_TASKS = 4


def note_candidate(text: str) -> dict | None:
    """The first groupable section of a note, or None.

    Groupable (all structural, all deterministic):
    - not a kanban board (boards are excluded from grouping entirely)
    - a named section whose non-blank lines are ALL task lines — prose or
      sub-headings mean the section isn't a flat list, so it's left alone
    - no indented tasks (nested lists have structure grouping would break)
    - at least MIN_TASKS tasks, every one stamped with a ^t- block id
      (proposals address members by id; staleness detection needs them)
    """
    fm, body = md.split_frontmatter(text)
    if "kanban-plugin" in fm:
        return None
    lines = body.splitlines()
    for heading in md.list_headings(body):
        loc = md.find_section(body, heading.lstrip("#").strip())
        if loc is None:
            continue
        start, end, level = loc
        section_lines = [ln for ln in lines[start + 1 : end] if ln.strip()]
        if len(section_lines) < MIN_TASKS:
            continue
        tasks = [tk.parse_task_line(ln) for ln in section_lines]
        if any(t is None for t in tasks):
            continue  # prose or sub-headings: not a flat task list
        if any(t.raw != t.raw.lstrip() for t in tasks):
            continue  # nested tasks: structure we must not flatten
        if any(t.block_id is None for t in tasks):
            continue  # unstamped: conformance first, grouping unlocked after
        return {
            "section": heading.lstrip("#").strip(),
            "level": level,
            "tasks": tasks,
        }
    return None


def validate_partition(groups: list[dict], expected_ids: list[str]) -> list[str]:
    """Integrity check before a drafter reply may become a proposal: the
    groups' member_ids must be exactly a partition of expected_ids —
    every input task exactly once, none invented, none dropped (the
    silently-missing-items-are-failures policy)."""
    issues: list[str] = []
    names = [str(g.get("name", "")).strip() for g in groups]
    if any(not n for n in names):
        issues.append("a group has an empty name")
    if len(set(n.lower() for n in names)) != len(names):
        issues.append("duplicate group names")
    seen: list[str] = []
    for g in groups:
        seen.extend(g.get("member_ids", []))
    dupes = {i for i in seen if seen.count(i) > 1}
    if dupes:
        issues.append(f"ids in more than one group: {sorted(dupes)}")
    invented = set(seen) - set(expected_ids)
    if invented:
        issues.append(f"invented ids: {sorted(invented)}")
    dropped = set(expected_ids) - set(seen)
    if dropped:
        issues.append(f"dropped ids: {sorted(dropped)}")
    return issues


def render_grouped(level: int, groups: list[dict], line_by_id: dict[str, str]) -> str:
    """Section content with one sub-heading per group and the original
    task lines byte-identical beneath it. Only call after
    validate_partition returned no issues."""
    sub = "#" * (level + 1)
    blocks = []
    for g in groups:
        blocks.append(
            "\n".join([f"{sub} {str(g['name']).strip()}", "", *[line_by_id[i] for i in g["member_ids"]]])
        )
    return "\n\n".join(blocks)
