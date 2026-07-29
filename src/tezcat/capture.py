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
messages. On its next pass the harness files it as a record: tasks
extracted and formatted, your original text kept verbatim beneath. A
plain task list files itself once verified; anything with a rewrite
asks on the dashboard first (unless its policy in Spec.md is full)._
"""


def capture_text(raw: str) -> str:
    """What the user typed: the note minus the skeleton's own lines.
    Known //command lines are never capture
    text — a command typed here by habit must not be extracted into a
    junk task (commands execute in knowledge notes; surfaces are
    excluded from the command scan)."""
    skeleton = {ln for ln in CAPTURE_SKELETON.splitlines() if ln.strip()}
    return "\n".join(
        ln for ln in raw.splitlines()
        if ln.strip() and ln not in skeleton and not is_command_line(ln)
    ).strip()
