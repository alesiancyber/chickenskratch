"""In-process proposal applier.

Approving a proposal executes its apply plan through the same vault
operations a chat turn uses — obsidian_mcp.server's tool functions are
plain callables in this venv, and every write guarantee (path safety,
git snapshots, loop-visible commits) lives in vault.py underneath them,
so nothing is re-implemented and no MCP round-trip is needed. The
checkbox already was the confirm.

Staleness: a proposal drafted Sunday must not blind-patch a note edited
Monday. Fast path: vault HEAD unchanged and tree clean ⇒ nothing moved.
Slow path: the target note's content hash still matches the draft ⇒ only
other notes changed. Anything else ⇒ stale, never applied.
"""

from __future__ import annotations

import difflib
import json
import os

import yaml

import obsidian_mcp.vault as vault_mod
from obsidian_mcp import markdown as md
from obsidian_mcp.vault import Vault

from .proposals import content_hash

# Mirrors the enum in config/schemas/proposal.yaml; checked again here so
# a hand-edited proposal file can't smuggle in an unlisted operation.
ALLOWED_TOOLS = {
    "update_frontmatter", "replace_section", "append_to_note",
    "add_task", "set_task_status", "add_card", "move_card", "create_note",
    "replace_line",
}


def use_vault(vault_path: str) -> None:
    """Point obsidian_mcp's vault singleton at this vault for in-process
    tool calls (the same env+reset idiom the test suite uses)."""
    os.environ["OBSIDIAN_VAULT_PATH"] = str(vault_path)
    vault_mod._vault = None


def vault_state(vault_path: str) -> tuple[str, str]:
    """(HEAD sha, `git status --porcelain` text). Empty sha if the repo has
    no commits yet; empty text = clean tree. Returning the porcelain output
    lets callers derive both cleanliness and a dirty-state hash from the
    one subprocess."""
    v = Vault(vault_path)
    v.ensure_repo()
    head = v._git("rev-parse", "HEAD")
    sha = head.stdout.strip() if head.returncode == 0 else ""
    return sha, v._git("status", "--porcelain").stdout


def is_stale(proposal: dict, vault_path: str, state: tuple[str, str] | None = None) -> str:
    """Why this proposal can't be applied, or "" if it's safe. Pass a
    precomputed `vault_state` when checking many proposals against the
    same unmoved tree."""
    sha, dirty = state if state is not None else vault_state(vault_path)
    clean = not dirty.strip()
    against = proposal["drafted_against"]
    if sha and sha == against["vault_sha"] and clean:
        return ""
    v = Vault(vault_path)
    try:
        current = v.read(against["note"])
    except vault_mod.VaultError:
        return f"note {against['note']} no longer exists"
    if content_hash(current) != against["content_hash"]:
        return f"{against['note']} changed since the proposal was drafted"
    return ""


def apply(proposal: dict, vault_path: str) -> list[dict]:
    """Execute the plan through the gated vault tools. Each step snapshots
    itself, so a mid-plan failure leaves per-step commits in ↺ Changes."""
    use_vault(vault_path)
    from obsidian_mcp import server  # after use_vault: tools resolve this vault

    results = []
    for step in proposal["apply"]:
        tool = step["tool"]
        if tool not in ALLOWED_TOOLS:
            raise ValueError(f"proposal {proposal['id']} names unlisted tool {tool!r}")
        results.append(getattr(server, tool)(**step["args"]))
    return results


def simulate(note_text: str, proposal: dict) -> str | None:
    """Apply the whole plan to note text in memory. None if any step can't
    be simulated — verifiers must then treat the plan as unvouchable."""
    current = note_text
    for step in proposal["apply"]:
        try:
            nxt = _simulate(current, step)
        except (ValueError, KeyError):
            return None
        if nxt is None:
            return None
        current = nxt
    return current


# ---- preview (pure text simulation for the diff in Review.md) ----

def _simulate(text: str, step: dict) -> str | None:
    """Apply one step to note text in memory. None = not previewable."""
    tool, args = step["tool"], step["args"]
    fm, body = md.split_frontmatter(text)
    if tool == "update_frontmatter":
        value = args["value"]
        # mirror the live tool exactly: strings parse as YAML, typed JSON
        # values (null, numbers) pass through — a simulate that diverges
        # from apply makes verifiers vouch for the wrong outcome
        fm[args["key"]] = yaml.safe_load(value) if isinstance(value, str) else value
        return md.join_frontmatter(fm, body)
    if tool == "replace_section":
        return md.join_frontmatter(fm, md.replace_section_text(body, args["heading"], args["content"]))
    if tool == "append_to_note":
        section = args.get("section", "")
        if section:
            body = md.append_to_section(body, section, args["content"])
        else:
            body = body.rstrip("\n") + "\n\n" + args["content"].strip("\n") + "\n"
        return md.join_frontmatter(fm, body)
    if tool == "replace_line":
        lines = body.splitlines()
        anchor = str(args["line_text"]).rstrip()
        hits = [i for i, ln in enumerate(lines) if ln.rstrip() == anchor]
        if len(hits) != 1:
            raise ValueError(f"line missing or ambiguous: {anchor!r}")
        lines[hits[0] : hits[0] + 1] = str(args["content"]).splitlines()
        return md.join_frontmatter(fm, "\n".join(lines) + "\n")
    return None


def simulate_for_note(note_text: str, proposal: dict) -> tuple[str, list[str]]:
    """Best-effort simulation of the plan against one note's text: steps
    that can't be simulated (other-note writes, unknown tools) are skipped
    and described, so nothing looks hidden. Returns (after, skipped)."""
    current, unpreviewable = note_text, []
    for step in proposal["apply"]:
        try:
            simulated = _simulate(current, step)
        except (ValueError, KeyError) as e:
            unpreviewable.append(f"{step['tool']}: preview failed ({e})")
            continue
        if simulated is None:
            unpreviewable.append(f"{step['tool']}({json_args(step)})")
        else:
            current = simulated
    return current, unpreviewable


def preview(note_text: str, proposal: dict) -> str:
    """Unified diff of the plan against the CURRENT note text; steps that
    can't be simulated are listed so nothing looks hidden."""
    current, unpreviewable = simulate_for_note(note_text, proposal)
    diff = "\n".join(
        difflib.unified_diff(
            note_text.splitlines(), current.splitlines(),
            fromfile=proposal["drafted_against"]["note"], tofile="after", lineterm="",
        )
    )
    notes = "\n".join(f"# not previewed: {u}" for u in unpreviewable)
    return "\n".join(x for x in (diff, notes) if x) or "# (no text change to preview)"


def json_args(step: dict) -> str:
    return json.dumps(step["args"], ensure_ascii=False)
