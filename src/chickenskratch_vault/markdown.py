"""Markdown structure helpers: YAML frontmatter and heading-anchored sections.

Section edits are the safe alternative to whole-file rewrites: callers
target a heading, and only the lines belonging to that section change.
"""

from __future__ import annotations

import re

import yaml

_FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter dict, body). Frontmatter absent -> ({}, text)."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(fm, dict):
        return {}, text
    return fm, text[m.end():]


def join_frontmatter(fm: dict, body: str) -> str:
    if not fm:
        return body
    dumped = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{dumped}\n---\n{body}"


def find_section(body: str, heading: str) -> tuple[int, int, int] | None:
    """Locate the section under `heading` (case-insensitive, any level).

    Returns (heading_line, end_line_exclusive, level) as indices into
    body.splitlines(), where the section runs until the next heading of
    the same or a higher level. None if the heading doesn't exist.
    """
    lines = body.splitlines()
    target = heading.strip().lstrip("#").strip().lower()
    start = level = None
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if not m:
            continue
        if start is None:
            if m.group(2).strip().lower() == target:
                start, level = i, len(m.group(1))
        elif len(m.group(1)) <= level:
            return start, i, level
    if start is None:
        return None
    return start, len(lines), level


def list_headings(body: str) -> list[str]:
    return [m.group(0).strip() for line in body.splitlines() if (m := _HEADING_RE.match(line))]


def replace_section_text(body: str, heading: str, new_content: str) -> str:
    """Replace the lines under `heading` (keeping the heading itself)."""
    loc = find_section(body, heading)
    if loc is None:
        available = ", ".join(list_headings(body)) or "(no headings)"
        raise ValueError(f"Heading not found: {heading!r}. Headings in note: {available}")
    start, end, _ = loc
    lines = body.splitlines()
    # One blank line of envelope on each side — never stacked onto blanks
    # the note already has (repeated rewrites were accreting blank runs).
    tail = lines[end:]
    new_lines = lines[: start + 1] + ["", *new_content.strip("\n").splitlines()]
    if tail and tail[0].strip():
        new_lines.append("")
    new_lines += tail
    return "\n".join(new_lines).rstrip("\n") + "\n"


def append_to_section(body: str, heading: str, addition: str) -> str:
    """Append lines at the end of the section under `heading`."""
    loc = find_section(body, heading)
    if loc is None:
        available = ", ".join(list_headings(body)) or "(no headings)"
        raise ValueError(f"Heading not found: {heading!r}. Headings in note: {available}")
    _, end, _ = loc
    lines = body.splitlines()
    # Insert above trailing blank lines so the addition sits inside the section.
    while end > 0 and end - 1 < len(lines) and lines[end - 1].strip() == "":
        end -= 1
    new_lines = lines[:end] + addition.strip("\n").splitlines() + lines[end:]
    return "\n".join(new_lines).rstrip("\n") + "\n"
