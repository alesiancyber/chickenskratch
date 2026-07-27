"""Cooperative cancellation for long-running, human-initiated work.

Cooperative — never asyncio.Task.cancel(). Two reasons:

- The MCP SDK's transports are anyio cancel scopes bound to the task
  that entered them, so hard-cancelling a chat turn rots the sessions
  unrecoverably (see pane.ChatSession).
- A pass mutates the vault through gated tools. Cancelling mid-plan
  would abandon a proposal half-applied — recoverable via snapshots,
  but the queue would then disagree with the vault.

So the long loops check a flag at points where stopping is ALREADY
safe: between agent iterations, between tool calls (with the model's
message sequence kept valid), between draft calls. Worst case latency
is one in-flight model call, which the worker and MCP timeouts already
bound — the UI can report "stopping…" and mean it.

Process-global, like the vault singleton: one UI server process owns at
most one pass and one chat turn, and the pass flock enforces the first
half of that.
"""

from __future__ import annotations

from collections.abc import Callable

PASS = "pass"    # an engine pass (scan/draft/apply)
CHAT = "chat"    # one escape-hatch pane turn
JOBS = (PASS, CHAT)

_requested: set[str] = set()


def request(job: str) -> None:
    """Ask `job` to stop at its next safe checkpoint."""
    _requested.add(job)


def clear(job: str) -> None:
    """Arm a fresh run. Every entry point clears before starting, so a
    stale request can never kill the next job the user asks for."""
    _requested.discard(job)


def is_requested(job: str) -> bool:
    return job in _requested


def token(job: str) -> Callable[[], bool]:
    """A zero-arg predicate the worker loops poll. Passed in like the
    other injection points (drafter, capture_extractor) so neither the
    engine nor the agent has to know this module exists."""
    return lambda: job in _requested
