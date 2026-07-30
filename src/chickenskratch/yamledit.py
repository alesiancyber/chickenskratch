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
scalars, no list editing. Anything past a scalar assignment belongs in
the raw editor, where the user owns the text and we only validate it.
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
