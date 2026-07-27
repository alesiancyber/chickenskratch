"""The capture surface's text contract: skeleton in, user text out.

Its own module because both sides of the capture pipeline need it from
opposite sides of a dependency edge — processors drafts against this
text, and applier's staleness check must normalize a capture proposal's
note exactly the same way (hashing the raw note would let a
whitespace-only autosave void an approval). A leaf module both can
import beats a lazy import buried in a function.
"""

from __future__ import annotations

from .commands import is_command_line

CAPTURE_SKELETON = """\
# Capture

_Type or paste anything here — brain dumps, meeting scraps, forwarded
messages. On its next pass the harness extracts tasks and proposes them
on the dashboard (or `tezcat decide`); nothing is added to the vault
without your approval._
"""

# Skeleton lines from earlier wordings of CAPTURE_SKELETON. Vaults seed
# the skeleton once and keep it until the next reset, so retired wording
# must stay stripped — otherwise it reads as user text and gets extracted
# into junk tasks.
_LEGACY_SKELETON_LINES = frozenset({
    "_Type or paste anything here — brain dumps, meeting scraps, forwarded",
    "messages. On its next pass the harness extracts tasks and proposes them",
    "in Review.md; nothing is added to the vault without your approval there._",
})


def capture_text(raw: str) -> str:
    """What the user typed: the note minus the skeleton's own lines
    (current or legacy wording). Known //command lines are never capture
    text — a command typed here by habit must not be extracted into a
    junk task (commands execute in knowledge notes; surfaces are
    excluded from the command scan)."""
    skeleton = {ln for ln in CAPTURE_SKELETON.splitlines() if ln.strip()} | _LEGACY_SKELETON_LINES
    return "\n".join(
        ln for ln in raw.splitlines()
        if ln.strip() and ln not in skeleton and not is_command_line(ln)
    ).strip()
