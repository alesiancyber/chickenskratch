"""The management console's read model and its writes.

Everything the harness reads as configuration has two homes, and the
split is not cosmetic — it decides who may change what and how a bad
change is undone:

- **The vault** (`Harness/Spec.md` frontmatter): how this vault behaves.
  Autonomy, the trust list, n_max, the folder map, the record folders. Edits
  land through the ordinary gated note write, so each one is a git
  snapshot the dashboard can revert, and the engine re-reads them next
  pass — no restart.
- **The repo** (`config/*.yaml`): what the harness IS. Endpoints, which
  env var holds which key, MCP server commands, entity schemas. Not in
  the vault, so not covered by the vault's git guarantee; writes here go
  through config_store, which validates then keeps the old bytes.

Two rules the console does not bend:

- **Secrets are never read back.** A profile declares `api_key_env`, the
  NAME of an env var. The console reports the name and whether the
  process has it set. The value never enters a response — a
  localhost-only page is still a page, and a key on screen is a key in
  a screenshot.
- **Validation happens before the write, against the real loader.** A
  models.yaml that doesn't parse takes down every pass and the chat pane
  with it, and the process that would report the failure is the one you
  just broke.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from chickenskratch_vault import markdown as md
from chickenskratch_vault import vaultconfig as vc
from chickenskratch_vault.vault import Vault, VaultError
from chickenskratch_vault.vaultconfig import SPEC_REL, get_config

from . import config_store, processors, profiles, yamledit
from .bootstrap import DEFAULT_MODELS, ROOT
from .config_store import ConfigError

MODELS_YAML = Path(DEFAULT_MODELS)

# The Spec.md keys the console owns. Everything here is a note edit the
# engine picks up next pass — no restart, no process state. Keys NOT
# listed (surfaces, schemas_dir) are machine concerns that live repo-side
# by design; the console does not pretend otherwise.
#
# What each setting actually changes, in the console's own words. Here
# rather than in the UI because these describe engine behaviour, and the
# engine is what has to keep being true — a description of a policy
# level that drifts from what the level does is worse than none.
FIELD_HELP = {
    "policies": (
        "One consent level per background processor — the level IS the "
        "gate, so nothing here can conflict with a setting elsewhere.\n\n"
        "off — it doesn't run at all.\n\n"
        "ask — every proposal waits for your verdict in “What needs a "
        "decision?”.\n\n"
        "verified — it applies only what its deterministic check can "
        "vouch for (your words, nothing invented, the verbatim complete); "
        "anything else queues. Processors with no check behave as ask.\n\n"
        "full — a standing grant for this lane: everything applies, with "
        "any objection recorded on the proposal, and every change one "
        "revertible snapshot. Anything the drafter itself flagged for "
        "review still waits, at every level.\n\n"
        "The preset above just fills these in — the levels are what is "
        "stored."),
    "disabled_processors": (
        "The //commands kill switch (\"inline_command\"). Processors are "
        "switched off with their policy level instead."),
    "fidelity": (
        "How closely an auto-applied extraction must match your own words.\n\n"
        "strict — word-for-word: every word in the task must appear in what "
        "you wrote.\n\n"
        "close — allows grammar and word-form changes (“call” → “called”), "
        "but never a word that changes the meaning, and the result must "
        "still follow the order of your line.\n\n"
        "Either way, failing the check doesn't lose the task — it queues "
        "it for you instead."),
    "n_max": (
        "How many proposals one pass may draft. This is a budget per pass, "
        "not a cap on the backlog: a big backlog drains over several "
        "passes. It exists to bound how much the model is asked to do at "
        "once, since drafting is the expensive part."),
    "worker": (
        "Which models.yaml profile drafts proposals in the background, for "
        "this vault. A profile NAME — endpoints and keys stay repo-side, "
        "so a vault can pick a model but never point the harness at a new "
        "server. Empty means the repo default. Re-read every pass."),
    "done_archive": (
        "Where completed tasks are moved out of the working task notes. "
        "Leave empty to switch archiving off and let done tasks pile up "
        "in place."),
    "capture_archive": (
        "Where a dump with no shape worth naming becomes its record \u2014 "
        "one note per day holding the summary, the formatted tasks, and "
        "your verbatim text folded beneath. Meeting-shaped dumps become "
        "notes under the meetings folder instead.\n\nEmpty falls back to "
        "Captures: the record is the destination, so filing can't be "
        "switched off."),
    "daily_dir": "The folder your daily notes live in.",
    "meetings_dir": (
        "The folder your meeting notes live in. Today's meetings list "
        "reads from here, and meeting-shaped dumps are filed here."),
    "exclude": (
        "Top-level folders no model ever sees — not the chat pane, not the "
        "workers, not the cloud. The hard privacy boundary, enforced on "
        "every read path rather than asked of the model."),
    "folders": (
        "The default note type for each folder, used when a note doesn't "
        "declare its own. A note's own frontmatter always wins; this only "
        "fills the gap, and it's what the schema fixer checks against."),
}

CHOICES = {"fidelity": ("strict", "close")}
PATHS = ("done_archive", "capture_archive", "daily_dir", "meetings_dir")
LISTS = ("disabled_processors", "exclude")

# Fields the Models tab edits as controls. Deliberately only scalars the
# console can set surgically (see yamledit): adding or removing a whole
# profile is a structural change and belongs in the raw editor, where the
# user owns the text and we only validate it.
PROFILE_FIELDS = ("worker_adapt", "worker_max_tokens", "confirm_writes",
                  "max_iterations", "max_tool_result_chars", "temperature")


# The //commands phase. Not a processor — it executes typed orders in
# place and is intrinsically trusted (I3), so it can be switched OFF but
# never "trusted to apply": it never queues anything to trust.
INLINE_COMMAND = {
    "name": "inline_command",
    "title": "//commands",
    "blurb": "The phase that runs commands you type into a note (//azure …) "
             "and writes the answer back beside the question. Your orders, "
             "not the harness's suggestions — they execute immediately and "
             "never enter the review queue.",
    "verified": True,
}


def processor_names() -> list[str]:
    """Every name Spec.md may legally contain, for validation."""
    return [p.name for p in processors.REGISTRY] + [INLINE_COMMAND["name"]]


def _processor_rows() -> list[dict]:
    """How each processor introduces itself, straight off the registry —
    the only place that knows what it does. `verified` says whether a
    deterministic check exists, which decides whether the UI offers the
    `verified` policy level at all — a level that silently means `ask`
    is worse than one that isn't offered."""
    return [{"name": p.name, "title": p.label, "blurb": p.blurb,
             "verified": p.verify is not None} for p in processors.REGISTRY]


# ---- read ----


def _spec_fields(vault_path: str) -> dict:
    """The effective spec, plus which keys the note actually SETS.

    Both halves matter on the surface: a value shown without saying
    whether it is yours or inherited invites you to "change" something
    that was never written down, and to be surprised when a default
    moves under you later.
    """
    cfg = get_config(vault_path)
    try:
        fm, _ = md.split_frontmatter(Vault(vault_path).read(SPEC_REL))
    except Exception:
        fm = {}
    effective = {
        "fidelity": cfg.fidelity, "n_max": cfg.n_max,
        "worker": cfg.worker,
        # RESOLVED levels, so the UI shows what the engine will actually
        # do — never a raw map that means something else combined with
        # keys the user can't see.
        "policies": {p.name: cfg.policy_for(p.name) for p in processors.REGISTRY},
        "disabled_processors": list(cfg.disabled_processors),
        "exclude": list(cfg.exclude),
        "folders": dict(cfg.folder_types),
        **{k: getattr(cfg, k) for k in PATHS},
    }
    # `folders` is the Spec.md key; the dataclass calls it folder_types —
    # `effective` uses the note's spelling so the two line up here.
    return {"values": effective, "declared": sorted(set(fm) & set(effective))}


def _profile_rows(config: profiles.Config) -> list[dict]:
    """One row per declared profile. `api_key_env` is reported by NAME
    and by whether the environment has it — never by value."""
    rows = []
    for name, p in config.models.items():
        rows.append({
            "name": name,
            "model": p.model,
            "base_url": p.base_url,
            "servers": p.servers,
            "is_default": name == config.default_model,
            "is_worker": name == config.worker,
            # the key: which var, and is it there. Not the secret.
            "api_key_env": p.api_key_env,
            "key_set": bool(os.environ.get(p.api_key_env)) if p.api_key_env else None,
            # A key typed straight into models.yaml instead of named via
            # api_key_env. Flagged, never shown: the file is on disk in a
            # repo, and the console is the one place likely to notice.
            "literal_key": bool(p.api_key and p.api_key != "ollama" and not p.api_key_env),
            "fields": {f: getattr(p, f) for f in PROFILE_FIELDS},
        })
    return rows


def _file_row(path: Path) -> dict:
    return {
        "name": path.name,
        # repo-relative when it is in the repo, which it is in every real
        # deployment; the absolute path is a fine fallback and never a crash
        "path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
        "backups": [{"stamp": b.stamp, "when": b.when}
                    for b in config_store.history(path)],
    }


def model(vault_path: str) -> dict:
    """Everything the console renders, in one read."""
    try:
        config = profiles.load_config(MODELS_YAML)
        models_error = ""
    except Exception as e:                      # a broken file must still render
        config, models_error = None, f"{type(e).__name__}: {e}"
    return {
        "vault": {
            "path": vault_path,
            "spec_note": SPEC_REL,
            **_spec_fields(vault_path),
            "choices": {k: list(v) for k, v in CHOICES.items()},
            # Two lists, because the two settings do not take the same
            # names: //commands can be switched off, but "trusting" it is
            # meaningless — it never queues anything to trust.
            "trustable": _processor_rows(),
            "disablable": [*_processor_rows(), INLINE_COMMAND],
            "help": FIELD_HELP,
        },
        "models": {
            "error": models_error,
            "default": config.default_model if config else "",
            "worker": config.worker if config else "",
            "profiles": _profile_rows(config) if config else [],
            "backends": sorted(config.backends) if config else [],
            "profile_fields": list(PROFILE_FIELDS),
            "file": _file_row(MODELS_YAML),
            "text": MODELS_YAML.read_text(encoding="utf-8"),
        },
    }


# ---- validate ----


def _check_path(key: str, value: str) -> None:
    if value in ("", None):
        if key in ("done_archive",):        # empty legitimately means "off"
            return
        raise ConfigError(f"{key} cannot be empty")
    p = str(value)
    if p.startswith("/") or ".." in Path(p).parts:
        raise ConfigError(f"{key} must be a path inside the vault, got {p!r}")
    if key == "done_archive" and not p.endswith(".md"):
        raise ConfigError(f"{key} must be a note path ending in .md, got {p!r}")


def validate_spec(fields: dict, vault_path: str) -> dict:
    """Reject what the engine would otherwise take and quietly ignore.

    This is most of the console's value over editing the frontmatter by
    hand. `_spec_overrides` is deliberately forgiving — a bad policy
    level keeps the default, an unknown processor name is reported and
    dropped — because a typo must never break every pass. That is right
    for the engine and useless as feedback: you asked for something and
    nothing happened. Here it is an error with a reason.
    """
    known = set(processor_names())
    clean: dict = {}
    for key, value in fields.items():
        if key in CHOICES:
            if value not in CHOICES[key]:
                raise ConfigError(f"{key} must be one of {list(CHOICES[key])}, got {value!r}")
            clean[key] = value
        elif key == "n_max":
            try:
                n = int(value)
            except (TypeError, ValueError):
                raise ConfigError(f"n_max must be a whole number, got {value!r}") from None
            if not 1 <= n <= 50:
                raise ConfigError(f"n_max must be between 1 and 50, got {n}")
            clean[key] = n
        elif key == "worker":
            name = str(value or "").strip()
            if name:
                try:
                    declared = set(profiles.load_config(MODELS_YAML).models)
                except Exception as e:
                    raise ConfigError(f"cannot read models.yaml to check the worker: {e}") from None
                if name not in declared:
                    raise ConfigError(
                        f"no model profile named {name!r} — declared: {sorted(declared)}")
            clean[key] = name
        elif key == "policies":
            if not isinstance(value, dict):
                raise ConfigError("policies must be a processor -> level map")
            levels = {}
            for name, level in value.items():
                if str(name) not in known or str(name) == "inline_command":
                    raise ConfigError(
                        f"policies: unknown processor {name!r} — known: "
                        f"{sorted(known - {'inline_command'})}")
                if str(level) not in vc.POLICY_LEVELS:
                    raise ConfigError(
                        f"policies[{name}] must be one of {list(vc.POLICY_LEVELS)}, "
                        f"got {level!r}")
                levels[str(name)] = str(level)
            # The whole map or nothing: saving `policies` drops the legacy
            # posture keys (see save_spec), so an unnamed processor would
            # silently fall to the bare default instead of what the user
            # sees on screen.
            missing = sorted(known - {"inline_command"} - set(levels))
            if missing:
                raise ConfigError(f"policies must name every processor; missing {missing}")
            clean[key] = levels
        elif key == "disabled_processors":
            names = [str(n) for n in (value or [])]
            unknown = [n for n in names if n not in known]
            if unknown:
                raise ConfigError(f"{key}: unknown processor(s) {unknown} — known: {sorted(known)}")
            clean[key] = names
        elif key == "exclude":
            clean[key] = [str(e).strip("/ ") for e in (value or []) if str(e).strip("/ ")]
        elif key == "folders":
            if not isinstance(value, dict):
                raise ConfigError("folders must be a folder -> type map")
            clean[key] = {str(k): str(v) for k, v in value.items()}
        elif key in PATHS:
            _check_path(key, value)
            clean[key] = str(value).strip()
        else:
            raise ConfigError(f"{key!r} is not a console-managed setting")
    return clean


def validate_models_yaml(text: str) -> None:
    """Parse it the way the harness will, in a scratch file, and insist
    the two profiles the system cannot start without still resolve."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"not valid YAML: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError("models.yaml must be a mapping")
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "models.yaml"
        scratch.write_text(text, encoding="utf-8")
        try:
            config = profiles.load_config(scratch)
        except Exception as e:
            raise ConfigError(f"{type(e).__name__}: {e}") from None
    if config.default_model not in config.models:
        raise ConfigError(
            f"default: {config.default_model!r} names no profile — chat would not start")
    if config.worker and config.worker not in config.models:
        raise ConfigError(
            f"worker: {config.worker!r} names no profile — every draft would fail")


# ---- write ----


def save_spec(vault_path: str, fields: dict) -> dict:
    """Write the console's Spec.md keys through the ordinary gated note
    write: locked, snapshot-bracketed, revertible from the changes feed
    like any other edit. The BODY is untouched — it is the prose that
    explains these keys, and it is the user's."""
    from .watcher import spec_seed

    clean = validate_spec(fields, vault_path)
    v = Vault(vault_path)
    with v.locked():
        try:
            text, existed = v.read(SPEC_REL), True
        except VaultError:
            # A vault the engine has never passed over has no Spec.md yet.
            # Seed it from the same helper the engine's first pass uses,
            # rather than refusing to configure a brand-new vault.
            text, existed = spec_seed(vault_path), False
        fm, body = md.split_frontmatter(text)
        merged = {**fm, **clean}
        if existed and merged == fm:
            return {"changed": [], "spec": SPEC_REL}
        changed = sorted(k for k in clean if fm.get(k) != clean[k])
        v.snapshot_before("console_spec")
        v.write(SPEC_REL, md.join_frontmatter(merged, body))
        v.snapshot_after(
            f"console: {', '.join(changed) or 'seed'} in {SPEC_REL}")
    return {"changed": changed, "spec": SPEC_REL, "seeded": not existed}


def save_model_fields(patch: dict) -> dict:
    """Set scalars in models.yaml surgically — comments, ordering and
    every untouched byte survive (see yamledit).

    `patch` is {"default": name} / {"worker": name} for the top-level
    picks, and {"profiles": {name: {field: value}}} for per-profile
    fields. Validated as a whole AFTER editing, so a patch that would
    leave the file unloadable is refused before it lands.
    """
    text = MODELS_YAML.read_text(encoding="utf-8")
    declared = set(yaml.safe_load(text).get("models") or {})
    for key in ("default", "worker"):
        if key in patch:
            name = str(patch[key] or "").strip()
            if key == "default" and not name:
                raise ConfigError("default: chat needs a profile")
            if name and name not in declared:
                raise ConfigError(f"no model profile named {name!r}")
            text = yamledit.set_scalar(text, [key], name)
    for name, fields in (patch.get("profiles") or {}).items():
        if name not in declared:
            raise ConfigError(f"no model profile named {name!r}")
        for field, value in fields.items():
            if field not in PROFILE_FIELDS:
                raise ConfigError(f"{field!r} is not a console-managed profile field")
            if field == "worker_adapt" and value not in ("none", "small"):
                raise ConfigError("worker_adapt must be 'none' or 'small'")
            if field in ("worker_max_tokens", "max_iterations", "max_tool_result_chars"):
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise ConfigError(f"{field} must be a whole number") from None
                if value < 0:
                    raise ConfigError(f"{field} cannot be negative")
            if field == "temperature":
                value = float(value)
            text = yamledit.set_scalar(text, ["models", name, field], value)
    config_store.write(MODELS_YAML, text, validate=validate_models_yaml)
    return {"restart": _restart_needed(patch)}


def save_models_text(text: str) -> dict:
    """The raw editor: the user owns the bytes, we own the refusal."""
    config_store.write(MODELS_YAML, text, validate=validate_models_yaml)
    return {"restart": True}


def restore_models(stamp: str) -> dict:
    config_store.restore(MODELS_YAML, stamp)
    return {"restart": True}


def _restart_needed(patch: dict) -> bool:
    """Which edits the running process will and won't notice.

    The worker profile is re-resolved every pass (bootstrap
    .apply_worker_profile), so worker changes are live. `default` picks
    the chat profile, and backends/servers are resolved once at startup —
    those need a restart, and saying so is the difference between a
    setting that looks broken and one that is simply not loaded yet.
    """
    if "default" in patch:
        return True
    touched = {f for fields in (patch.get("profiles") or {}).values() for f in fields}
    # worker_* fields ride the per-pass worker env; the rest configure
    # agents that were constructed at startup.
    return bool(touched - {"worker_adapt", "worker_max_tokens"})
