"""In-process proposal applier.

Approving a proposal executes its apply plan through the same vault
operations a chat turn uses — tezcat_vault.server's tool functions are
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
import hashlib
import json
import os
from pathlib import Path

import yaml

import tezcat_vault.vault as vault_mod
from tezcat_vault import markdown as md
from tezcat_vault.vault import Vault
from tezcat_vault.vaultconfig import get_config

from .capture import capture_text
from .proposals import content_hash

# Mirrors the enum in config/schemas/proposal.yaml; checked again here so
# a hand-edited proposal file can't smuggle in an unlisted operation.
ALLOWED_TOOLS = {
    "update_frontmatter", "replace_section", "append_to_note",
    "add_task", "set_task_status", "add_card", "move_card", "create_note",
    "replace_line", "archive_done_tasks",
}


def use_vault(vault_path: str) -> None:
    """Point tezcat_vault's vault singleton at this vault for in-process
    tool calls (the same env+reset idiom the test suite uses).

    DO NOT make this idempotent. Skipping the reset when the path is
    unchanged looks like free work — apply() calls this per plan — but
    the reset is load-bearing: Vault captures cfg.exclude at
    construction, so dropping the singleton is what makes a Spec.md
    `exclude` edit take effect on the next pass without a restart (I4).
    An early return here would silently keep the harness reading a
    folder the user just excluded. Rebuilding is cheap — get_config is
    mtime-cached — so pay it.
    """
    os.environ["TEZCAT_VAULT_PATH"] = str(vault_path)
    vault_mod._vault = None


def vault_state(vault_path: str) -> tuple[str, str]:
    """(HEAD sha, a text fingerprint of everything not yet committed).
    Empty sha if the repo has no commits yet; empty text = clean tree.

    Three sources, and each one is load-bearing:

    - `status --porcelain` — which paths are dirty or untracked.
    - `diff HEAD` — the CONTENT of tracked edits. Porcelain alone lists
      paths, so a second edit to an already-dirty note left the pass
      fingerprint unchanged and the engine skipped the scan; an armed
      //command in a dirty inbox never retried (2026-07-24).
    - content hashes of UNTRACKED files — the same hole, one step
      further out. Porcelain reports `?? Tasks/typing.md` identically no
      matter what the file says, so typing a //command into a brand-new
      note left the fingerprint unmoved and the command never fired.
      This was masked until 2026-07-26: the engine re-rendered its
      markdown surfaces every pass, and those commits swept untracked
      notes into git as a side effect. Retiring the surfaces removed the
      accidental heartbeat and exposed the real bug.
    """
    v = Vault(vault_path)
    v.ensure_repo()
    head = v._git("rev-parse", "HEAD")
    sha = head.stdout.strip() if head.returncode == 0 else ""
    porcelain = v._git("status", "--porcelain").stdout
    dirty = porcelain
    if sha:
        dirty += v._git("diff", "HEAD").stdout
    for line in porcelain.splitlines():
        if not line.startswith("?? "):
            continue
        rel = line[3:].strip().strip('"')
        p = v.root / rel
        try:
            # Tolerant by design (I6): an unreadable or vanished file
            # must cost this line its precision, never the whole pass.
            if p.is_file():
                dirty += f"\n{rel}:{hashlib.sha1(p.read_bytes()).hexdigest()}"
        except OSError:
            continue
    return sha, dirty


def is_stale(proposal: dict, vault_path: str, state: tuple[str, str] | None = None) -> str:
    """Why this proposal can't be applied, or "" if it's safe. Pass a
    precomputed `vault_state` when checking many proposals against the
    same unmoved tree."""
    sha, dirty = state if state is not None else vault_state(vault_path)
    clean = not dirty.strip()
    against = proposal["drafted_against"]
    if sha and sha == against["vault_sha"] and clean:
        return ""
    # WITH the exclusions: staleness reads a note's content, so it is a
    # read like any other and I4 applies. Every other content-reading
    # construction passes these; this one didn't, which made the queue the
    # one place an excluded folder was still legible.
    v = Vault(vault_path, exclude=get_config(vault_path).exclude)
    try:
        current = v.read(against["note"])
    except vault_mod.VaultError:
        return f"note {against['note']} no longer exists"
    if proposal.get("kind") == "capture":
        # Capture proposals hash the stripped text (see draft_capture):
        # whitespace-only churn from autosave must not void an approval.
        current = capture_text(current)
    if content_hash(current) != against["content_hash"]:
        return f"{against['note']} changed since the proposal was drafted"
    return ""


def apply(proposal: dict, vault_path: str) -> list[dict]:
    """Execute the plan through the gated vault tools. Each step snapshots
    itself, so a mid-plan failure leaves per-step commits in ↺ Changes."""
    use_vault(vault_path)
    from tezcat_vault import server  # after use_vault: tools resolve this vault

    results = []
    for step in proposal["apply"]:
        tool = step["tool"]
        if tool not in ALLOWED_TOOLS:
            raise ValueError(f"proposal {proposal['id']} names unlisted tool {tool!r}")
        results.append(getattr(server, tool)(**step["args"]))
    return results


def same_note(a: str, b: str) -> bool:
    """Do two vault-relative paths name the same note? Mirrors resolve()'s
    normalization (suffix-optional, leading slashes) without touching disk."""
    def norm(p: str) -> str:
        p = str(p).strip().lstrip("/")
        return p if Path(p).suffix else p + ".md"
    return norm(a) == norm(b)


def simulate(note_text: str, proposal: dict, note: str | None = None) -> str | None:
    """Apply the whole plan to note text in memory. None if ANY step can't
    be simulated — verifiers must then treat the plan as unvouchable.
    `note` is the path the text belongs to (default: the proposal's
    drafted_against note): a step aimed at any OTHER path is unvouchable,
    because simulating it here would vouch for an edit that apply() will
    make somewhere else entirely.

    The strict reading of simulate_for_note: same walk, but any skipped
    step voids the whole answer instead of being described. Two copies of
    that walk is how a verifier and a preview drift apart."""
    after, skipped = simulate_for_note(note_text, proposal, note)
    return None if skipped else after


# ---- preview (pure text simulation for the diff in Review.md) ----

_SIMULABLE = {"update_frontmatter", "replace_section", "append_to_note", "replace_line"}


def _simulate(text: str, step: dict, note: str) -> str | None:
    """Apply one step to `note`'s text in memory. None = not previewable
    (a tool this can't model); raises ValueError when the step targets a
    different note — previewing it against THIS text would be a lie."""
    tool, args = step["tool"], step["args"]
    if tool not in _SIMULABLE:
        return None
    target = args.get("path")
    if target is None or not same_note(str(target), note):
        raise ValueError(f"targets {target!r}, not the drafted note {note!r}")
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


def simulate_for_note(note_text: str, proposal: dict, note: str | None = None) -> tuple[str, list[str]]:
    """Best-effort simulation of the plan against one note's text: steps
    that can't be simulated (other-note writes, unknown tools) are skipped
    and described, so nothing looks hidden. Returns (after, skipped).

    The one plan walk — `simulate` is this with "any skip voids it"."""
    note = note or proposal.get("drafted_against", {}).get("note") or ""
    if not note:
        # No target at all: every step is aimed elsewhere by definition.
        return note_text, [f"{s['tool']}: no note to simulate against"
                           for s in proposal["apply"]]
    current, unpreviewable = note_text, []
    for step in proposal["apply"]:
        try:
            simulated = _simulate(current, step, note)
        except Exception as e:
            # Any exception, not just ValueError/KeyError. Simulation is
            # best-effort by definition: a step it cannot model is
            # "not previewed", never a raise. A worker plan carrying
            # malformed YAML in update_frontmatter threw yaml.ScannerError
            # straight through here — into the dashboard's /api/review
            # (a 500 that empties the whole queue view) and, once this walk
            # became the verifier's too, into the auto-apply gate.
            unpreviewable.append(f"{step['tool']}: preview failed "
                                 f"({type(e).__name__}: {e})")
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
