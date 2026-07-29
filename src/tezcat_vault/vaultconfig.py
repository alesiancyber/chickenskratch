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
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "vault.yaml"

SPEC_REL = "Harness/Spec.md"

# The per-processor consent levels, least to most autonomous. See
# VaultConfig.policies / policy_for().
POLICY_LEVELS = ("off", "ask", "verified", "full")


# The out-of-box consent levels for processors a vault's `policies` map
# doesn't name: the mechanically-checkable lanes apply what their
# verifier vouches for; everything else asks. One home, shared by
# policy_for and the Spec seed.
DEFAULT_POLICIES = {
    "capture": "verified",
    "schema_fix": "verified",
    "archive_done": "verified",
}


@dataclass(frozen=True)
class VaultConfig:
    folder_types: dict[str, str]
    exclude: tuple[str, ...]
    surfaces: dict[str, str]
    schemas_dir: Path
    # Where completed tasks get filed out of the working task notes (the
    # archive_done processor). Empty string disables archiving.
    done_archive: str = "Tasks/Archive.md"
    # Folder of dated capture records — where a dump with no shape worth
    # naming (a bare task list) lives, one note per day: summary if any,
    # formatted task lines, and the verbatim dump quoted beneath.
    # Meeting-shaped dumps become notes under meetings_dir instead.
    # Empty falls back to "Captures": the record IS the destination, so
    # there is no opting out of filing.
    capture_archive: str = "Captures"
    # Per-processor consent policy — THE gating truth, one level per
    # processor, so there is no second knob for a level to conflict with:
    #   off      — the processor doesn't run at all
    #   ask      — every proposal queues for a human verdict
    #   verified — applies when its deterministic verifier passes;
    #              a processor with no verifier behaves as ask
    #   full     — applies regardless; a failing (or absent) verifier's
    #              objections are recorded on the proposal
    #              (verifier_failures) — the standing grant, per lane
    # `needs_review` (a drafter flagging something a human must see)
    # forces the queue at every level, staleness still guards, and every
    # apply remains one revertible snapshot; reverts are the negative
    # labels. Processors the map doesn't name take DEFAULT_POLICIES.
    policies: dict[str, str] = field(default_factory=dict)
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

    def policy_for(self, name: str) -> str:
        """One processor's effective consent level (off|ask|verified|full).

        `disabled_processors` is checked FIRST: it is the emergency kill
        switch ("reach for this while you work out why"), and a seeded
        `policies` map naming every processor must never render it inert.
        Then the `policies` map is the truth; a processor it doesn't
        name takes DEFAULT_POLICIES (ask, when unlisted there too). One
        resolver, so the engine and the console can never read the same
        Spec differently."""
        if name in self.disabled_processors:
            return "off"
        level = self.policies.get(name)
        if level in POLICY_LEVELS:
            return level
        return DEFAULT_POLICIES.get(name, "ask")


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
             "done_archive", "capture_archive"}
    ignored = sorted(set(raw) - known)
    if ignored:
        # Keys like policies/fidelity/n_max are Spec.md-only; silence
        # here would let a user believe an edit took effect. stderr, not
        # stdout: the MCP server's stdout is the JSON-RPC stream.
        print(f"{p.name}: ignoring keys {ignored} — these live in the vault's "
              f"Harness/Spec.md frontmatter, not here", file=sys.stderr)
    return VaultConfig(
        folder_types={str(k): str(v) for k, v in (raw.get("folders") or {}).items()},
        exclude=tuple(str(e) for e in (raw.get("exclude") or [])),
        surfaces={str(k): str(v) for k, v in (raw.get("surfaces") or {}).items()},
        schemas_dir=p.parent / str(raw.get("schemas_dir") or "schemas"),
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
    if isinstance(fm.get("done_archive"), str):  # empty string = archiving off
        out = replace(out, done_archive=fm["done_archive"].strip())
    if isinstance(fm.get("capture_archive"), str):  # empty string = no filing
        out = replace(out, capture_archive=fm["capture_archive"].strip())
    if isinstance(fm.get("policies"), dict):
        # Junk levels are dropped, not defaulted: an invalid level falls
        # through to DEFAULT_POLICIES rather than silently meaning
        # something the user didn't write.
        out = replace(out, policies={
            str(k): str(v) for k, v in fm["policies"].items()
            if str(v) in POLICY_LEVELS})
    if fm.get("fidelity") in ("strict", "close"):
        out = replace(out, fidelity=str(fm["fidelity"]))
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
