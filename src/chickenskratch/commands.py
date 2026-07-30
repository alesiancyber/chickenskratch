"""Inline //commands — the vault is the command line.

Commands are the second class of vault change, distinct from proposals.
A proposal is a change the harness INFERRED: it earns trust through a
human verdict or a verifier, queues under data/, and whole-note
staleness guards the diff a human approved. A command is a change the
user ORDERED by typing it: trust is intrinsic, nothing queues, and
staleness is scoped to the one line the order anchors on —
replace_line's exactly-once anchor IS the staleness check. Both classes
share the same substrate: gated vault tools, snapshot-per-mutation,
the changes log, chickenskratch-revert.

A line starting with a known //command at column 0 of any non-surface
note is executed on the next engine pass and replaced in place by its
result, rendered as a markdown callout. Enter is the submit key: a
final line with no newline after it is still being typed. Only KNOWN
command names match, so prose and code comments starting with // are
never touched. LLM-backed commands (//ask) write directly too — the
user is actively watching the spot where the output lands.

Two fast lanes share this grammar and executor: the canvas splits
command lines out of a dump and runs them instantly, and the note
editor's ⌘↩ runs the line under the caret via /api/command — one
submit gesture meaning the same thing on every surface.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

_CMD_RE = re.compile(r"^//([a-z][\w-]*)\s+(\S.*)$")
_CMD_PREFIX_RE = re.compile(r"^//([a-z][\w-]*)\b")

RESULT_MAX_CHARS = 6000


def is_command_line(line: str) -> bool:
    """Does this line start with a KNOWN command name (args or not)?
    Capture and inbox extraction use this to keep commands out of task
    intent."""
    m = _CMD_PREFIX_RE.match(line)
    return bool(m and m.group(1) in INLINE_COMMANDS)


def parse(line: str) -> tuple[str, str] | None:
    """(name, args) when the line is a complete known command, else None.
    The canvas submit path splits its dumps with this the way scan
    matches note lines — one grammar, two entry points."""
    m = _CMD_RE.match(line)
    if m and m.group(1) in INLINE_COMMANDS:
        return m.group(1), m.group(2).strip()
    return None


async def _azure_docs(args: str, vault_path: str) -> str | None:
    """//azure: one ms-learn MCP search, digested to a ranked link list
    (mcp_client's docs digest — links, never summaries), model-free.
    None = transient failure (server unreachable, no search tool): the
    command line stays put and retries on the next scan."""
    from .bootstrap import DEFAULT_MCP
    from .mcp_client import MultiServerClient, load_mcp_config

    try:
        specs = load_mcp_config(DEFAULT_MCP, vault_path)
        async with MultiServerClient(specs, server_filter=["ms-learn"]) as client:
            tool = next((t for t in client.tools if "search" in t), None)
            if tool is None:
                return None
            text, is_error = await client.call(tool, {"query": args})
    except Exception:
        return None
    if is_error:
        return None
    lines = text.splitlines()
    if lines and lines[0].startswith("Documentation pages found"):
        lines = lines[1:]  # model-facing instruction, not note content
    # Cap the block at a LINE boundary: a mid-line cut would sever a link.
    kept, size = [], 0
    for ln in lines:
        size += len(ln) + 1
        if size > RESULT_MAX_CHARS:
            break
        kept.append(ln)
    return "\n".join(kept).strip() or None


async def _ask(args: str, vault_path: str) -> str | None:
    """//ask: answer a question from the vault's own notes — the first
    LLM-backed command (query.py: deterministic retrieval, one bounded
    worker call, sources cited as [[links]]). None = transient failure
    (worker down or junk reply): in a note the line stays armed and
    retries next scan; on the canvas the receipt says to try again.
    The question is the user's ORDER (I3), so nothing queues — and the
    result is an answer written where they asked it, never a vault
    mutation beyond that."""
    from chickenskratch_vault.vault import VaultError

    from . import query
    try:
        return await query.ask(vault_path, args)
    except VaultError:
        return None


# command name -> async (args, vault_path) -> result markdown | None.
# //research is reserved for a future web-crawling command.
INLINE_COMMANDS: dict[str, Callable[[str, str], Awaitable[str | None]]] = {
    "azure": _azure_docs,
    "ask": _ask,
}


def scan(notes: dict[str, str]) -> list[dict]:
    """Typed commands awaiting execution: one per note per pass (a note
    with several resolves over successive passes). `notes` is the pass's
    shared content walk (Watcher.pass_notes) — rel -> text."""
    items = []
    for rel, text in notes.items():
        lines = text.splitlines()
        # Enter is the submit key: a final line with no newline after it
        # is still being typed — never execute a half-typed command.
        if text and not text.endswith("\n"):
            lines = lines[:-1]
        for line in lines:
            m = _CMD_RE.match(line)
            if m and m.group(1) in INLINE_COMMANDS:
                items.append({"note": rel, "line": line,
                              "command": m.group(1), "args": m.group(2).strip()})
                break
    return items


def render_callout(item: dict, result: str) -> str:
    """The result as a NOTE callout: a visually distinct titled block in
    common markdown previews (GitHub alerts require the uppercase form),
    plain text on disk. The title echoes the command (provenance) and,
    being blockquoted, can never re-match the command scan or read as
    inbox task intent."""
    quoted = "\n".join("> " + ln for ln in result.splitlines())
    return f"> [!NOTE] //{item['command']} {item['args']}\n{quoted}"
