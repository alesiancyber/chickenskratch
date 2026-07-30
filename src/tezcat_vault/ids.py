"""Entity identity primitives — the one id format.

An id is a review watermark: stamped when the
harness creates an entity, or when the user approves a normalization
from the review dashboard — never written by hand. Note-level entities
carry it in frontmatter; line-level entities (tasks, cards) carry it as
a trailing Obsidian block reference (` ^t-3f2a...`), which also makes
them linkable via [[Note#^t-3f2a...]].
"""

from __future__ import annotations

import re
import uuid

# Obsidian block refs: preceded by whitespace, last thing on the line.
# Requiring \s+ before ^ keeps trailing math like "2^10" intact.
BLOCK_REF_RE = re.compile(r"\s+\^(?P<ref>[A-Za-z0-9][A-Za-z0-9_-]*)\s*$")

# The harness's OWN stamped refs only — stamp_id's kind-prefixed shape
# (^t-3f2a…). A deliberate subset of BLOCK_REF_RE: verifiers strip these
# to recover "the text the user wrote, independent of stamping", and a
# hand-made ^myref is user text, not stamping — it must survive the
# strip. Anything matching this shape by hand is indistinguishable from
# a stamp and is treated as one.
TYPED_REF_RE = re.compile(r"\s*\^[a-z]-[a-z0-9]+\s*$")


def stamp_id(kind: str = "") -> str:
    """New 12-hex id, kind-prefixed for block refs: stamp_id("t") -> "t-3f2a..."."""
    core = uuid.uuid4().hex[:12]
    return f"{kind}-{core}" if kind else core


def split_block_ref(line: str) -> tuple[str, str | None]:
    """Strip a trailing block ref: ("- [ ] x ^t-1", ...) -> ("- [ ] x", "t-1")."""
    m = BLOCK_REF_RE.search(line)
    if not m:
        return line, None
    return line[: m.start()].rstrip(), m.group("ref")


def attach_block_ref(line: str, ref: str) -> str:
    """Append a block ref as the last token on the line (Obsidian requires last)."""
    return f"{line.rstrip()} ^{ref}"
