"""The management console's read model and its writes.

Everything the harness reads as configuration has two homes, and the
split is not cosmetic — it decides who may change what and how a bad
change is undone:

- **The vault** (`Harness/Spec.md` frontmatter): how this vault behaves.
  Autonomy, the trust list, n_max, the folder map, the inboxes. Edits
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

from tezcat_vault import markdown as md
from tezcat_vault.vault import Vault, VaultError
from tezcat_vault.vaultconfig import SPEC_REL, get_config

from . import config_store, processors, profiles, yamledit
from .bootstrap import DEFAULT_MODELS, ROOT
from .config_store import ConfigError

MODELS_YAML = Path(DEFAULT_MODELS)

# Spec.md keys the console owns, with how to check them. Everything here
# is a note edit the engine picks up next pass — no restart, no process
# state. Keys NOT listed (surfaces, schemas_dir) are machine concerns
# that live repo-side by design; the console does not pretend otherwise.
CHOICES = {"autonomy": ("gated", "full"), "fidelity": ("strict", "close")}
PATHS = ("capture_inbox", "waiting_inbox", "done_archive", "daily_dir", "meetings_dir")
LISTS = ("auto_apply", "disabled_processors", "exclude")

# Fields the Models tab edits as controls. Deliberately only scalars the
# console can set surgically (see yamledit): adding or removing a whole
# profile is a structural change and belongs in the raw editor, where the
# user owns the text and we only validate it.
PROFILE_FIELDS = ("worker_adapt", "worker_max_tokens", "confirm_writes",
                  "max_iterations", "max_tool_result_chars", "temperature")


def processor_names() -> list[str]:
    """Every name auto_apply / disabled_processors may contain. The
    console offers checkboxes rather than a free-text list precisely so a
    typo can't silently disable nothing."""
    return [p.name for p in processors.REGISTRY] + ["inline_command"]


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
        "autonomy": cfg.autonomy, "fidelity": cfg.fidelity, "n_max": cfg.n_max,
        "worker": cfg.worker,
        "auto_apply": list(cfg.auto_apply),
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
            "processors": processor_names(),
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
    if key.endswith("_inbox") or key == "done_archive":
        if not p.endswith(".md"):
            raise ConfigError(f"{key} must be a note path ending in .md, got {p!r}")


def validate_spec(fields: dict, vault_path: str) -> dict:
    """Reject what the engine would otherwise take and quietly ignore.

    This is most of the console's value over editing the frontmatter by
    hand. `_spec_overrides` is deliberately forgiving — a bad `autonomy:`
    keeps the old posture, an unknown processor name is reported and
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
        elif key in ("auto_apply", "disabled_processors"):
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
