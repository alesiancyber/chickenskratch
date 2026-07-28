"""Vault spec loader: folder→entity-type map, exclusions,
harness surfaces.

The vault describes itself: the user-facing keys (folders, inboxes,
exclusions) live in the frontmatter of `Harness/Spec.md` *inside the
vault* — editable like any note, read fresh each engine pass.
`config/vault.yaml` supplies the defaults a new vault's Spec.md is
seeded from (and the fallback when no vault is resolvable, e.g. the MCP
server starting cold). Machine concerns — surface paths, schemas_dir —
stay repo-side: moving a surface is a code decision, not a note edit.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "vault.yaml"

SPEC_REL = "Harness/Spec.md"


@dataclass(frozen=True)
class VaultConfig:
    folder_types: dict[str, str]
    exclude: tuple[str, ...]
    surfaces: dict[str, str]
    schemas_dir: Path
    capture_inbox: str = "Tasks/Inbox.md"  # where approved captures land
    waiting_inbox: str = "Tasks/Waiting.md"  # commitments others made to you (follow-ups)
    # Where completed tasks get filed out of the working inboxes (the
    # archive_done processor). Empty string disables archiving.
    done_archive: str = "Tasks/Archive.md"
    # Folder holding one dated note per day of processed capture dumps.
    # A dump is a RECORD, not just a source of tasks: approving its
    # extraction files the text here before clearing the surface, so the
    # narrative around the work survives somewhere the user reads.
    # Empty string disables filing — and then approving discards it.
    capture_archive: str = "Captures"
    # Processors trusted to apply without a human verdict — ONLY honored
    # for processors that ship a deterministic verifier, and only when it
    # passes; anything else falls back to the review queue. The trust
    # list is user-editable per vault in Harness/Spec.md.
    auto_apply: tuple[str, ...] = ("capture", "inbox_extract", "schema_fix", "archive_done")
    # Consent posture. "gated" (default): auto-apply requires a passing
    # deterministic verifier, so judgment always queues and a failing
    # verifier queues its proposal. "full": a standing human grant,
    # written per vault in Spec.md — EVERYTHING in auto_apply applies:
    # verifier-less judgment lanes included, and a failing verifier
    # applies anyway with its objections recorded on the proposal
    # (verifier_failures). needs_review still queues, staleness still
    # guards, and every apply remains one revertible snapshot; reverts
    # become the negative labels.
    autonomy: str = "gated"
    # How closely auto-applied extractions must match the user's text:
    # "strict" = word-for-word (every output word present in the input);
    # "close"  = grammar/punctuation allowance — added words must be
    # filler or morphology, meaning-flippers never pass, and the output
    # must sequence-match its source line. Verification failure keeps
    # the checkbox either way.
    fidelity: str = "close"
    # models.yaml profile the drafting workers run on ("" = the repo
    # default: models.yaml `worker:`, then WORKER_* env). A profile NAME,
    # never an endpoint — URLs and keys stay repo-side, the vault only
    # picks which declared profile to use. Swapping the worker model is
    # a note edit; the engine re-reads it every pass.
    worker: str = ""
    # Processors switched off for this vault (the rollback flag, now a
    # note edit). Unknown names are reported and ignored, never a crash.
    disabled_processors: tuple[str, ...] = ()
    # Proposals drafted per engine pass; a big backlog drains over days.
    n_max: int = 5
    # Folder for daily notes (if another editor's daily-notes plugin has
    # its own setting, point both at the same folder — the harness follows).
    daily_dir: str = "Daily"
    # Folder for meeting notes. A Spec.md key like daily_dir — the same
    # KIND of thing (where a note type lives), and every consumer (the
    # create tool, Today's meeting walks) must read it from here: a bare
    # "Meetings" literal means a user who renames the folder loses the
    # dashboard sections without being told why.
    meetings_dir: str = "Meetings"

    @property
    def surface_dirs(self) -> frozenset[str]:
        """Top-level folders holding machine-rendered surfaces — excluded
        from entity scans and grouping candidates. Derived here so the
        surfaces-aren't-knowledge rule has exactly one home."""
        return frozenset(s.split("/")[0] for s in self.surfaces.values())


def load_config(path: str | Path | None = None) -> VaultConfig:
    """The repo-side defaults (config/vault.yaml). Missing file falls back
    to safe built-ins (no folder map, Private/ excluded) so the MCP server
    still runs outside this repo."""
    p = Path(path or os.environ.get("VAULT_CONFIG_PATH") or _DEFAULT_PATH)
    if not p.exists():
        return VaultConfig(
            folder_types={}, exclude=("Private",), surfaces={}, schemas_dir=p.parent / "schemas"
        )
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    known = {"folders", "exclude", "surfaces", "schemas_dir",
             "capture_inbox", "waiting_inbox", "done_archive", "capture_archive"}
    ignored = sorted(set(raw) - known)
    if ignored:
        # Keys like auto_apply/fidelity/n_max are Spec.md-only; silence
        # here would let a user believe an edit took effect. stderr, not
        # stdout: the MCP server's stdout is the JSON-RPC stream.
        print(f"{p.name}: ignoring keys {ignored} — these live in the vault's "
              f"Harness/Spec.md frontmatter, not here", file=sys.stderr)
    return VaultConfig(
        folder_types={str(k): str(v) for k, v in (raw.get("folders") or {}).items()},
        exclude=tuple(str(e) for e in (raw.get("exclude") or [])),
        surfaces={str(k): str(v) for k, v in (raw.get("surfaces") or {}).items()},
        schemas_dir=p.parent / str(raw.get("schemas_dir") or "schemas"),
        capture_inbox=str(raw.get("capture_inbox") or "Tasks/Inbox.md"),
        waiting_inbox=str(raw.get("waiting_inbox") or "Tasks/Waiting.md"),
        done_archive=str(raw.get("done_archive", "Tasks/Archive.md") or ""),
        capture_archive=str(raw.get("capture_archive", "Captures") or ""),
    )


def _spec_overrides(base: VaultConfig, fm: dict) -> VaultConfig:
    """Apply a Spec.md frontmatter dict over the defaults. Only keys the
    note actually sets are overridden — a partial spec is fine."""
    out = base
    if isinstance(fm.get("folders"), dict):
        out = replace(out, folder_types={str(k): str(v) for k, v in fm["folders"].items()})
    if isinstance(fm.get("exclude"), list):
        out = replace(out, exclude=tuple(str(e) for e in fm["exclude"]))
    if isinstance(fm.get("capture_inbox"), str) and fm["capture_inbox"]:
        out = replace(out, capture_inbox=fm["capture_inbox"])
    if isinstance(fm.get("waiting_inbox"), str) and fm["waiting_inbox"]:
        out = replace(out, waiting_inbox=fm["waiting_inbox"])
    if isinstance(fm.get("done_archive"), str):  # empty string = archiving off
        out = replace(out, done_archive=fm["done_archive"].strip())
    if isinstance(fm.get("capture_archive"), str):  # empty string = no filing
        out = replace(out, capture_archive=fm["capture_archive"].strip())
    if isinstance(fm.get("auto_apply"), list):  # empty list = all queued
        out = replace(out, auto_apply=tuple(str(n) for n in fm["auto_apply"]))
    if fm.get("fidelity") in ("strict", "close"):
        out = replace(out, fidelity=str(fm["fidelity"]))
    if fm.get("autonomy") in ("gated", "full"):
        out = replace(out, autonomy=str(fm["autonomy"]))
    if isinstance(fm.get("worker"), str):
        out = replace(out, worker=fm["worker"].strip())
    if isinstance(fm.get("disabled_processors"), list):
        out = replace(out, disabled_processors=tuple(str(n) for n in fm["disabled_processors"]))
    if isinstance(fm.get("n_max"), int) and not isinstance(fm.get("n_max"), bool) and fm["n_max"] >= 1:
        out = replace(out, n_max=int(fm["n_max"]))
    if isinstance(fm.get("daily_dir"), str) and fm["daily_dir"].strip():
        out = replace(out, daily_dir=fm["daily_dir"].strip().strip("/"))
    if isinstance(fm.get("meetings_dir"), str) and fm["meetings_dir"].strip():
        out = replace(out, meetings_dir=fm["meetings_dir"].strip().strip("/"))
    return out


_base_cache: dict[str, VaultConfig] = {}
# vault root -> (spec-note mtime, effective config). Keyed by root, not a
# single slot: one checkout legitimately serves several vaults (the engine
# and the UI server both key their state by vault_digest), and a one-entry
# cache thrashed into a re-read on every alternating call.
_merged_cache: dict[str, tuple[float | None, VaultConfig]] = {}


def get_config(vault_path: str | None = None) -> VaultConfig:
    """The effective spec for a vault: repo defaults with the vault's own
    `Harness/Spec.md` frontmatter applied over them. Cached per
    (vault, spec-note mtime) — editing the note takes effect on the
    next engine pass, no restart."""
    base_key = str(os.environ.get("VAULT_CONFIG_PATH") or _DEFAULT_PATH)
    if base_key not in _base_cache:
        _base_cache[base_key] = load_config()
    base = _base_cache[base_key]

    root = vault_path or os.environ.get("TEZCAT_VAULT_PATH") or ""
    if not root:
        return base
    spec = Path(root) / SPEC_REL
    try:
        mtime: float | None = spec.stat().st_mtime
    except OSError:
        mtime = None
    key = str(Path(root))
    hit = _merged_cache.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    cfg = base
    if mtime is not None:
        from . import markdown as md  # local import: markdown is dependency-free

        try:
            fm, _body = md.split_frontmatter(spec.read_text(encoding="utf-8"))
            if isinstance(fm, dict):
                cfg = _spec_overrides(base, fm)
        except (OSError, yaml.YAMLError):
            pass  # unreadable/corrupt spec note: defaults, never a crash
    _merged_cache[key] = (mtime, cfg)
    return cfg
