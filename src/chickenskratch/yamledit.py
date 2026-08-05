"""Set one scalar in a YAML file without disturbing anything else.

`config/models.yaml` is more comment than config — why the cloud switch
is deliberately on, what I4 costs a cloud profile, why a 4B model gets
`confirm_writes`. Round-tripping it through `yaml.safe_dump` to change
`worker: ` would silently delete all of it, and the console's whole job
is to make config editing safer than opening the file by hand.

So this edits text, not a parse tree: find the line, replace the value,
leave every byte of structure, ordering, blank line and comment alone —
the same bargain `tasks._set_line_date` strikes with a task line. It
handles block mappings nested by indentation, which is all these config
files are. It is NOT a YAML writer: no flow collections, no multi-line
scalars, no list editing. Structural work is limited to whole blocks of
scalars (insert_block/delete_block — how the console creates or removes
a profile); anything past that belongs in the raw editor, where the
user owns the text and we only validate it.
"""

from __future__ import annotations

import re

import yaml


class PathError(ValueError):
    """The requested key path doesn't address a block mapping we can edit."""


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _comment_at(after_colon: str) -> int:
    """Index of the trailing comment's `#`, or -1.

    Quote-aware: a `#` inside a quoted value is part of the value, not a
    comment. Rare in practice and cheap to get right; getting it wrong
    truncates someone's URL at a fragment.
    """
    quote = ""
    for i, ch in enumerate(after_colon):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and i > 0 and after_colon[i - 1] in " \t":
            return i
    return -1


def _format(value) -> str:
    """Render a scalar the way PyYAML would read it back."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    text = str(value)
    # safe_dump round-trips the quoting rules (empty string, "yes", "3",
    # leading %, …) so we never have to enumerate them here.
    dumped = yaml.safe_dump(text, default_flow_style=True).strip()
    return dumped.removesuffix("...").strip() or "''"


def _find_block(lines: list[str], key: str, start: int, end: int,
                indent: int) -> tuple[int, int, int]:
    """Locate `key:` among the mapping entries in [start, end) at
    `indent`. Returns (key line, block end, child indent) — block end is
    exclusive and covers the key's nested lines."""
    pattern = re.compile(rf"^ {{{indent}}}{re.escape(key)}:(?!\S)")
    for i in range(start, end):
        if not _is_content(lines[i]) or _indent_of(lines[i]) != indent:
            continue
        if not pattern.match(lines[i]):
            continue
        stop = end
        for j in range(i + 1, end):
            if _is_content(lines[j]) and _indent_of(lines[j]) <= indent:
                stop = j
                break
        child = next((_indent_of(lines[j]) for j in range(i + 1, stop)
                      if _is_content(lines[j])), indent + 2)
        return i, stop, child
    raise PathError(f"no key {key!r} at indent {indent}")


def set_scalar(text: str, path: list[str], value) -> str:
    """Set `path` (e.g. ["worker"] or ["models", "qwen3.5-4b", "n_max"])
    to `value`, returning the new text.

    A missing LEAF key is appended to its parent block — that is how a
    profile gains a field it never declared. A missing key anywhere
    above the leaf is an error: inventing a whole profile is a
    structural change, not a scalar assignment.
    """
    if not path:
        raise PathError("empty path")
    lines = text.splitlines()
    start, end, indent = 0, len(lines), 0
    for key in path[:-1]:
        _, end, indent = _find_block(lines, key, start, end, indent)
        start = _ + 1

    leaf = path[-1]
    try:
        at, _stop, _child = _find_block(lines, leaf, start, end, indent)
    except PathError:
        # Append to the parent block, after its last content line, so the
        # new key lands inside the block rather than after its comments.
        last = max((i for i in range(start, end) if _is_content(lines[i])),
                   default=start - 1)
        lines.insert(last + 1, f"{' ' * indent}{leaf}: {_format(value)}")
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")

    head, _, rest = lines[at].partition(":")
    hash_at = _comment_at(rest)
    if hash_at < 0:
        lines[at] = f"{head}: {_format(value)}"
    else:
        # Re-pad to the comment's ORIGINAL column so the file stays
        # aligned. At least one space, always: `worker: local# note` is
        # not a comment at all — YAML reads the whole run as the value,
        # so losing that space silently corrupts the setting.
        body = f"{head}: {_format(value)}"
        column = len(head) + 1 + hash_at
        lines[at] = body + " " * max(1, column - len(body)) + rest[hash_at:].rstrip()
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _walk(lines: list[str], path: list[str]) -> tuple[int, int, int]:
    """(start, end, child indent) of the block `path` addresses — the
    range its entries live in. Raises PathError when any step is missing:
    inventing intermediate structure is not this module's business."""
    start, end, indent = 0, len(lines), 0
    for key in path:
        at, end, indent = _find_block(lines, key, start, end, indent)
        start = at + 1
    return start, end, indent


def insert_block(text: str, parent_path: list[str], key: str, mapping: dict) -> str:
    """Add `key:` with a flat mapping of scalars under the parent block —
    how a whole profile or backend is created. Lists of strings render
    flow-style ([a, b]) since that is how these files write them. The key
    must not already exist; every byte outside the insertion survives."""
    lines = text.splitlines()
    start, end, indent = _walk(lines, parent_path)
    try:
        _find_block(lines, key, start, end, indent)
    except PathError:
        pass  # absent, as an insert requires
    else:
        raise PathError(f"{key!r} already exists under {'/'.join(parent_path)}")
    block = [f"{' ' * indent}{key}:"]
    for k, v in mapping.items():
        if isinstance(v, list):
            rendered = "[" + ", ".join(_format(x) for x in v) + "]"
        else:
            rendered = _format(v)
        block.append(f"{' ' * (indent + 2)}{k}: {rendered}")
    # After the parent's last content line, like set_scalar's append
    # branch — inside the block, not after its trailing comments.
    last = max((i for i in range(start, end) if _is_content(lines[i])),
               default=start - 1)
    lines[last + 1:last + 1] = block
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def delete_block(text: str, parent_path: list[str], key: str) -> str:
    """Remove `key:` and its nested lines from the parent block, plus the
    run of full-line comments immediately above it at the same indent —
    a comment explaining a profile must not outlive the profile and
    appear to describe its neighbor."""
    lines = text.splitlines()
    start, end, indent = _walk(lines, parent_path)
    at, stop, _child = _find_block(lines, key, start, end, indent)
    # _find_block's span runs to the next content line, so a LAST entry
    # swallows the trailing sibling-level comments after it. Trim those
    # back out: a comment nested under the key (deeper indent) documents
    # its fields and goes with it; one at the key's indent or shallower
    # is a sibling remark and stays.
    while stop > at + 1:
        s = lines[stop - 1].strip()
        if not s or (s.startswith("#") and _indent_of(lines[stop - 1]) <= indent):
            stop -= 1
        else:
            break
    first = at
    while first - 1 >= start:
        prev = lines[first - 1].strip()
        if prev.startswith("#") and _indent_of(lines[first - 1]) == indent:
            first -= 1
        elif not prev:
            # The blank separating the block from its neighbor goes too,
            # or every delete leaves a widening gap.
            first -= 1
        else:
            break
    del lines[first:stop]
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
