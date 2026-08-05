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

import asyncio
import os
import re
import tempfile
from pathlib import Path

import yaml

from chickenskratch_vault import jsonval
from chickenskratch_vault import markdown as md
from chickenskratch_vault import schemas as entity_schemas
from chickenskratch_vault import vaultconfig as vc
from chickenskratch_vault import workers as wk
from chickenskratch_vault.vault import Vault, VaultError
from chickenskratch_vault.vaultconfig import SPEC_REL, get_config

from . import config_store, processors, profiles, yamledit
from .bootstrap import DEFAULT_MODELS, ROOT
from .config_store import ConfigError

MODELS_YAML = Path(DEFAULT_MODELS)

# Ceiling on one console test-run, independent of the spec's own
# timeout_s (which build_worker allows up to an hour) and of any
# WORKER_TIMEOUT_S override. This call runs in Starlette's threadpool,
# which is bounded and shared with every sync read route on the
# dashboard — a handful of test-runs waiting out a cold or wedged
# endpoint would otherwise starve /api/today and the note editor. A
# tuning round-trip that takes longer than this is a failure worth
# reporting anyway.
TEST_RUN_TIMEOUT_S = 90.0

# The Spec.md keys the console owns. Everything here is a note edit the
# engine picks up next pass — no restart, no process state. The AI layer
# (config/workers/, config/schemas/, models.yaml) lives repo-side but is
# console-managed too — the Behaviors/Schemas/Models tabs; only the DIR
# locations themselves (schemas_dir, workers_dir, surfaces) stay
# hand-edited machine concerns.
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
PROFILE_FIELDS = ("worker_adapt", "worker_max_tokens", "worker_timeout_s",
                  "confirm_writes", "max_iterations", "max_tool_result_chars",
                  "temperature")


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
        "path": _rel(path),
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
        "workers": {
            "dir": _rel(Path(wk.workers_dir())),
            "reserved": sorted(wk.RESERVED_WORKERS),
            "items": _worker_rows(),
        },
        "schemas": {
            "dir": _rel(_schemas_dir()),
            "engine": sorted(ENGINE_SCHEMAS),
            "dir_keys": list(DIR_KEYS),
            "items": _schema_rows(vault_path),
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


def _clean_profile_field(field: str, value):
    """One editable profile field, checked and coerced — shared by the
    per-field save and profile creation so the two can't disagree."""
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
    if field in ("temperature", "worker_timeout_s"):
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{field} must be a number") from None
        if field == "worker_timeout_s" and value < 0:
            raise ConfigError(f"{field} cannot be negative")
    return value


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
            text = yamledit.set_scalar(text, ["models", name, field],
                                       _clean_profile_field(field, value))
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
    return bool(touched - {"worker_adapt", "worker_max_tokens", "worker_timeout_s"})


# ---- the AI layer: worker specs and entity schemas ----
#
# Both live as repo-side YAML the engine reads fresh each pass (mtime-
# keyed caches in chickenskratch_vault.workers/.schemas), so every write
# here answers {"restart": False}: the next pass runs the new config.
# Validation is the real loader in both cases — build_worker and the
# schemas loader's own checks — so a file that saves is a file that
# loads, and a refusal names the reason.

# Schemas the machinery itself depends on: proposal.yaml validates the
# proposal files, and the line types (task/card/deliverable) describe
# parsed lines. Editable — carefully — but never deletable.
ENGINE_SCHEMAS = frozenset({"proposal"}) | frozenset(entity_schemas.LINE_TYPES)

# The dir_key choices offered in the UI — the loader's own list
# (parse_record_block enforces it), re-exported for the model payload.
DIR_KEYS = entity_schemas.DIR_KEYS

# The date shape every task surface parses; the form's "date" field kind
# compiles to it.
_DATE_PATTERN = "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def _schemas_dir() -> Path:
    return Path(vc.get_config().schemas_dir)


class _Lit(str):
    """A string yaml.dump renders as a | literal block."""


class _ConsoleDumper(yaml.SafeDumper):
    pass


_ConsoleDumper.add_representer(
    _Lit, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", str(s), style="|"))


def _dump_yaml(doc) -> str:
    return yaml.dump(doc, Dumper=_ConsoleDumper, sort_keys=False,
                     allow_unicode=True, width=100000)


def _lit(text: str) -> _Lit:
    """Literal-block text. Trailing whitespace is stripped per line
    because a literal block cannot represent it — PyYAML would silently
    fall back to a quoted dump, and the byte-fidelity promise of | is
    the whole point."""
    return _Lit("\n".join(ln.rstrip() for ln in str(text).split("\n")))


def _worker_path(name: str) -> Path:
    if not entity_schemas.NAME_RE.fullmatch(name or ""):
        raise ConfigError("worker names are lowercase letters, digits and underscores")
    return Path(wk.workers_dir()) / f"{name}.yaml"


def _schema_path(name: str) -> Path:
    if not entity_schemas.NAME_RE.fullmatch(name or ""):
        raise ConfigError("schema names are lowercase letters, digits and underscores")
    return _schemas_dir() / f"{name}.yaml"


def _worker_rows() -> list[dict]:
    """One row per worker file. A broken file still renders — raw text
    plus the loader's refusal — because the console is where you go to
    FIX it."""
    rows = []
    d = Path(wk.workers_dir())
    for name in wk.worker_names():
        path = d / f"{name}.yaml"
        text = path.read_text(encoding="utf-8")
        row = {"name": name, "reserved": name in wk.RESERVED_WORKERS,
               "invalid": "", "text": text, "file": _file_row(path)}
        try:
            spec = wk.build_worker(name, text)
        except VaultError as e:
            row["invalid"] = str(e)
            rows.append(row)
            continue
        row.update({
            "description": spec.description,
            "inputs": list(spec.inputs),
            "timeout_s": spec.timeout_s,
            "temperature": spec.temperature,
            "max_tokens": spec.max_tokens,
            "prompt": spec.prompt,
            "hedges": spec.hedges,
            "schema_text": _dump_yaml(spec.schema),
        })
        try:
            composed = wk.compose_kinds(spec)
            if composed is not spec:
                row["generated"] = {
                    "prompt": composed.prompt,
                    "kind_enum": composed.schema["properties"]["record"]
                                                ["properties"]["kind"]["enum"],
                }
        except VaultError as e:
            row["invalid"] = str(e)
        rows.append(row)
    rows.extend(_deleted_rows(d))
    return rows


def _deleted_rows(d: Path) -> list[dict]:
    """Rows for files deleted since their last backup — name plus the
    restore list, nothing else. The delete toast promises the backups
    remain restorable; these rows are the surface that keeps it."""
    return [{"name": Path(name).stem, "deleted": True, "invalid": "",
             "file": _file_row(d / name)}
            for name in config_store.removed(d)]


def _schema_rows(vault_path: str) -> list[dict]:
    folder_types = get_config(vault_path).folder_types
    rows = []
    for path in sorted(_schemas_dir().glob("*.yaml")):
        name = path.stem
        text = path.read_text(encoding="utf-8")
        row = {"name": name, "engine": name in ENGINE_SCHEMAS,
               "invalid": "", "text": text, "file": _file_row(path)}
        try:
            doc = yaml.safe_load(text)
            if not isinstance(doc, dict):
                raise ConfigError("a schema file is a mapping")
        except (yaml.YAMLError, ConfigError) as e:
            row["invalid"] = str(e)
            rows.append(row)
            continue
        meta, doc = entity_schemas.split_metadata(doc)
        form = _form_rows(doc) if doc else []
        row.update({
            "description": str(meta.get("description") or "").strip(),
            "id_prefix": str(meta.get("id_prefix") or ""),
            "record": meta.get("record"),
            "schema_text": _dump_yaml(doc) if doc else "",
            "formable": form is not None,
            "form": form,
            "used_by": sorted(f for f, t in folder_types.items() if t == name),
        })
        rows.append(row)
    rows.extend(_deleted_rows(_schemas_dir()))
    return rows


# ---- the field-list form <-> jsonschema ----


def _compile_form(fields: list[dict]) -> dict:
    """The form's rows as the jsonschema they mean. The reverse of
    _form_rows; a schema is offered as a form only when the round trip
    is lossless, so the form can never silently rewrite a file."""
    props: dict = {}
    required: list[str] = []
    for row in fields or []:
        fname = str(row.get("name") or "").strip()
        if not fname:
            raise ConfigError("every field needs a name")
        if fname in props:
            raise ConfigError(f"field {fname!r} declared twice")
        kind = str(row.get("kind") or "string")
        nullable = bool(row.get("nullable"))
        p: dict = {}
        if kind in ("string", "date"):
            p["type"] = ["string", "null"] if nullable else "string"
            pattern = _DATE_PATTERN if kind == "date" else str(row.get("pattern") or "")
            if pattern:
                p["pattern"] = pattern
            if row.get("min_length"):
                p["minLength"] = int(row["min_length"])
        elif kind == "number":
            p["type"] = ["number", "null"] if nullable else "number"
        elif kind == "boolean":
            p["type"] = ["boolean", "null"] if nullable else "boolean"
        elif kind == "array":
            p = {"type": "array", "items": {"type": "string"}}
        elif kind == "enum":
            vals = [v for v in (row.get("enum") or []) if v not in ("", None)]
            if not vals:
                raise ConfigError(f"{fname}: an enum field needs values")
            p["enum"] = [*vals, None] if nullable else vals
        elif kind == "const":
            p["const"] = row.get("const")
        else:
            raise ConfigError(f"{fname}: unknown field kind {kind!r}")
        props[fname] = p
        if row.get("required"):
            required.append(fname)
    doc: dict = {"type": "object"}
    if required:
        doc["required"] = required
    doc["properties"] = props
    return doc


def _form_rows(doc: dict) -> list[dict] | None:
    """The schema as form rows, or None when the shape is beyond the
    form (nested objects, combinators, extra keywords). Verified by
    round-trip: only a schema _compile_form reproduces exactly is
    formable — anything else stays raw-editor-only."""
    if not isinstance(doc, dict) or set(doc) - {"type", "required", "properties"}:
        return None
    if doc.get("type") != "object":
        return None
    required = doc.get("required") or []
    rows: list[dict] = []
    for fname, p in (doc.get("properties") or {}).items():
        if not isinstance(p, dict):
            return None
        row = {"name": fname, "required": fname in required, "nullable": False,
               "kind": "string", "pattern": "", "min_length": 0,
               "enum": [], "const": None}
        keys = set(p)
        if keys == {"const"}:
            row["kind"], row["const"] = "const", p["const"]
        elif keys == {"enum"}:
            vals = list(p["enum"])
            row["nullable"] = None in vals
            row["kind"], row["enum"] = "enum", [v for v in vals if v is not None]
            # Non-string values would come back from the form's text
            # input as strings — a silent type rewrite. Raw editor only.
            if not all(isinstance(v, str) for v in row["enum"]):
                return None
        elif p.get("type") == "array":
            if keys - {"type", "items"} or p.get("items") != {"type": "string"}:
                return None
            row["kind"] = "array"
        else:
            t = p.get("type")
            if isinstance(t, list):
                if "null" in t and len(t) == 2:
                    row["nullable"] = True
                    t = next(x for x in t if x != "null")
                else:
                    return None
            if t == "boolean":
                if keys - {"type"}:
                    return None
                row["kind"] = "boolean"
            elif t == "number":
                if keys - {"type"}:
                    return None
                row["kind"] = "number"
            elif t == "string":
                if keys - {"type", "pattern", "minLength"}:
                    return None
                pattern = str(p.get("pattern") or "")
                if pattern == _DATE_PATTERN:
                    row["kind"] = "date"
                else:
                    row["pattern"] = pattern
                row["min_length"] = int(p.get("minLength") or 0)
            else:
                return None
        rows.append(row)
    return rows if _compile_form(rows) == doc else None


# ---- worker writes ----


def _worker_doc(fields: dict) -> dict:
    """The form's fields as the canonical worker document. Rendering is
    server-side on purpose: the frontend carries no YAML library, and
    one renderer means one file shape."""
    doc: dict = {}
    desc = str(fields.get("description") or "").strip()
    if desc:
        doc["description"] = desc
    doc["inputs"] = [str(i).strip() for i in (fields.get("inputs") or []) if str(i).strip()]
    for key, cast in (("timeout_s", float), ("temperature", float), ("max_tokens", int)):
        value = fields.get(key)
        if value in (None, ""):
            continue
        try:
            doc[key] = cast(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{key} must be a number") from None
    doc["prompt"] = _lit(fields.get("prompt") or "")
    hedges = str(fields.get("hedges") or "")
    if hedges.strip():
        doc["hedges"] = _lit(hedges)
    schema = fields.get("schema")
    if isinstance(schema, str):
        try:
            schema = yaml.safe_load(schema)
        except yaml.YAMLError as e:
            raise ConfigError(f"reply schema: not valid YAML: {e}") from None
    doc["schema"] = schema
    return doc


def save_worker(name: str, text: str | None = None, fields: dict | None = None) -> dict:
    """Create or update one worker file. `text` is the raw editor;
    `fields` is the form, rendered to canonical YAML here. Either way
    the real loader validates before a byte lands."""
    name = str(name or "").strip()
    if text is None:
        if fields is None:
            raise ConfigError("nothing to save")
        text = _dump_yaml(_worker_doc(fields))
    try:
        spec = wk.build_worker(name, text)
        wk.compose_kinds(spec)   # a {kinds} prompt must compose today, not at 2am
    except VaultError as e:
        raise ConfigError(str(e)) from None
    path = _worker_path(name)
    config_store.write(path, text)
    return {"restart": False, "file": _rel(path)}


def delete_worker(name: str) -> dict:
    if name in wk.RESERVED_WORKERS:
        raise ConfigError(
            f"{name!r} is an engine worker — the pipeline invokes it by name. "
            "Edit it instead; deleting it would fail every pass that needs it.")
    path = _worker_path(name)
    config_store.remove(path)
    return {"restart": False, "file": _rel(path)}


def restore_worker(name: str, stamp: str) -> dict:
    path = _worker_path(name)
    config_store.restore(path, stamp)
    return {"restart": False, "file": _rel(path)}


def seed_worker(name: str) -> dict:
    """A starting file for a new behavior — enough structure that the
    first save teaches the contract instead of failing it."""
    _worker_path(name)  # name rule
    doc = {
        "description": "What this worker is for, in a sentence.",
        "inputs": ["text"],
        "prompt": _Lit(
            "Say what the model must do with the input below.\n\n"
            "Return ONLY a JSON object of this shape, nothing else:\n"
            '{{"result": "..."}}\n\n'
            "Text:\n{text}\n"),
        "schema": {"type": "object", "required": ["result"],
                   "properties": {"result": {"type": "string", "minLength": 1}}},
    }
    return {"name": name, "text": _dump_yaml(doc)}


def test_worker(name: str, inputs: dict, text: str | None = None,
                fields: dict | None = None) -> dict:
    """Run one worker against pasted sample input — the draft (raw text
    or form fields) when given, else the saved file — on the current
    worker profile: try before commit. A worker-side failure comes back
    as the result, not an HTTP error: a schema-invalid reply IS the
    useful output when you're tuning a prompt."""
    if text is None and fields is not None:
        text = _dump_yaml(_worker_doc(fields))
    try:
        spec = wk.build_worker(str(name or ""), text) if text is not None \
            else wk.get_worker(str(name or ""))
        spec = wk.compose_kinds(spec)
    except VaultError as e:
        raise ConfigError(str(e)) from None
    given = {str(k): str(v) for k, v in (inputs or {}).items()}
    missing = sorted(set(spec.inputs) - set(given))
    if missing:
        raise ConfigError(f"missing input(s) {missing}")
    fill = {k: given[k] for k in spec.inputs}
    rendered = wk.render_prompt(spec, **fill)

    async def _call():
        # A fresh client per test: the module singleton is bound to the
        # engine thread's event loop, and this runs in the threadpool.
        client = wk.new_client()
        try:
            return await asyncio.wait_for(
                wk.run_worker(spec, client=client, **fill), TEST_RUN_TIMEOUT_S)
        finally:
            await client.close()

    out = {"prompt": rendered, "model": wk.worker_model(), "adapt": wk.worker_adapt()}
    try:
        out["reply"] = asyncio.run(_call())
    except VaultError as e:
        out["error"] = str(e)
    except TimeoutError:
        out["error"] = (
            f"no reply within {TEST_RUN_TIMEOUT_S:.0f}s — the endpoint is cold, "
            f"wedged, or the prompt is too large for it "
            f"({wk.worker_base_url()})")
    return out


# ---- schema writes ----


def _schema_doc(fields: dict) -> dict:
    doc: dict = {}
    desc = str(fields.get("description") or "").strip()
    if desc:
        doc["description"] = desc
    prefix = str(fields.get("id_prefix") or "").strip()
    if prefix:
        doc["id_prefix"] = prefix
    record = fields.get("record")
    if isinstance(record, dict):
        block: dict = {"kind": str(record.get("kind") or "").strip()}
        if record.get("default"):
            block["default"] = True
        block["files_as"] = str(record.get("files_as") or "daily-append")
        if record.get("dir_key"):
            block["dir_key"] = str(record["dir_key"])
        block["cues"] = _lit(record.get("cues") or "")
        if str(record.get("guidance") or "").strip():
            block["guidance"] = _lit(record["guidance"])
        doc["record"] = block
    schema_fields = fields.get("fields")
    if schema_fields:
        doc.update(_compile_form(list(schema_fields)))
    elif isinstance(fields.get("schema"), str) and fields["schema"].strip():
        try:
            parsed = yaml.safe_load(fields["schema"])
        except yaml.YAMLError as e:
            raise ConfigError(f"schema: not valid YAML: {e}") from None
        if not isinstance(parsed, dict):
            raise ConfigError("schema must be a mapping")
        doc.update(parsed)
    return doc


def validate_schema_text(name: str, text: str) -> None:
    """The schemas loader's checks, run against a candidate before it
    lands — plus the cross-file record rules (unique kinds, exactly one
    default) that only make sense against the rest of the directory."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"not valid YAML: {e}") from None
    if not isinstance(doc, dict) or not doc:
        raise ConfigError("a schema file is a mapping")
    meta, doc = entity_schemas.split_metadata(doc)
    if doc:
        issues = jsonval.check_schema(doc)
        if issues:
            raise ConfigError(f"not a valid jsonschema: {issues[0]}")
    if name in ENGINE_SCHEMAS and not doc:
        raise ConfigError(f"{name!r} is an engine schema — it cannot be emptied to metadata only")
    candidate = None
    if meta.get("record") is not None:
        try:
            candidate = entity_schemas.parse_record_block(name, meta["record"])
        except VaultError as e:
            raise ConfigError(str(e)) from None
    # The cross-file invariants, with this file's candidate in place of
    # its saved self. Broken sibling files are skipped — one bad file
    # must not block fixing another.
    others: dict[str, entity_schemas.RecordKind] = {}
    for p in sorted(_schemas_dir().glob("*.yaml")):
        if p.stem == name:
            continue
        try:
            block = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("record")
            if isinstance(block, dict):
                other = entity_schemas.parse_record_block(p.stem, block)
                others[other.name] = other
        except (yaml.YAMLError, VaultError, OSError):
            continue
    if candidate is not None and candidate.name in others:
        raise ConfigError(f"record kind {candidate.name!r} is already declared by another schema")
    issue = entity_schemas.kinds_default_issue(
        list(others.values()) + ([candidate] if candidate else []))
    if issue:
        raise ConfigError(f"{issue} — this save would break that")


def save_schema(name: str, text: str | None = None, fields: dict | None = None) -> dict:
    name = str(name or "").strip()
    path = _schema_path(name)
    if text is None:
        if fields is None:
            raise ConfigError("nothing to save")
        text = _dump_yaml(_schema_doc(fields))
    validate_schema_text(name, text)
    config_store.write(path, text)
    return {"restart": False, "file": _rel(path)}


def delete_schema(name: str, vault_path: str) -> dict:
    if name in ENGINE_SCHEMAS:
        raise ConfigError(
            f"{name!r} is an engine schema — proposals and task lines validate "
            "against it; deleting it would break every pass.")
    path = _schema_path(name)
    used = sorted(f for f, t in get_config(vault_path).folder_types.items() if t == name)
    if used:
        raise ConfigError(
            f"folder(s) {used} map to type {name!r} — remap them in the Vault tab first")
    if path.is_file():
        try:
            block = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("record")
            if isinstance(block, dict) and \
                    entity_schemas.parse_record_block(name, block).default:
                raise ConfigError(
                    "this is the DEFAULT record kind — every unmatched dump files "
                    "through it. Make another kind the default first.")
        except (yaml.YAMLError, VaultError):
            pass  # a broken file can always be deleted
    config_store.remove(path)
    return {"restart": False, "file": _rel(path)}


def restore_schema(name: str, stamp: str) -> dict:
    path = _schema_path(name)
    config_store.restore(path, stamp)
    return {"restart": False, "file": _rel(path)}


# ---- profile / backend create + delete ----


def create_profile(name: str, fields: dict) -> dict:
    """A new models.yaml profile as a validated textual insert — every
    byte outside the new block survives, and a render bug is a refusal
    (validate_models_yaml runs on the result), never a broken file."""
    name = str(name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ConfigError("profile names are letters, digits, dots, dashes")
    text = MODELS_YAML.read_text(encoding="utf-8")
    if name in set(yaml.safe_load(text).get("models") or {}):
        raise ConfigError(f"profile {name!r} already exists")
    fields = dict(fields or {})
    mapping: dict = {}
    model_id = str(fields.pop("model", "") or "").strip()
    if not model_id:
        raise ConfigError("a profile needs a model id")
    mapping["model"] = model_id
    base_url = str(fields.pop("base_url", "") or "").strip()
    if not base_url:
        raise ConfigError("a profile needs a base_url endpoint")
    mapping["base_url"] = base_url
    key_env = str(fields.pop("api_key_env", "") or "").strip()
    if key_env:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key_env):
            raise ConfigError(
                "api_key_env is the NAME of an environment variable (e.g. "
                "MY_API_KEY) — never paste the key itself")
        mapping["api_key_env"] = key_env
    for field, value in fields.items():
        if value in (None, ""):
            continue
        mapping[field] = _clean_profile_field(field, value)
    try:
        text = yamledit.insert_block(text, ["models"], name, mapping)
    except yamledit.PathError as e:
        raise ConfigError(str(e)) from None
    config_store.write(MODELS_YAML, text, validate=validate_models_yaml)
    return {"restart": True}


def delete_profile(name: str, vault_path: str) -> dict:
    text = MODELS_YAML.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    if name not in set(raw.get("models") or {}):
        raise ConfigError(f"no model profile named {name!r}")
    if name == raw.get("default"):
        raise ConfigError(f"{name!r} is the chat default — pick another default first")
    if name == (raw.get("worker") or ""):
        raise ConfigError(f"{name!r} is the worker profile — pick another worker first")
    if name == get_config(vault_path).worker:
        raise ConfigError(
            f"the vault's Spec.md names {name!r} as its worker — change that first")
    try:
        text = yamledit.delete_block(text, ["models"], name)
    except yamledit.PathError as e:
        raise ConfigError(str(e)) from None
    config_store.write(MODELS_YAML, text, validate=validate_models_yaml)
    return {"restart": True}


def create_backend(name: str, fields: dict) -> dict:
    name = str(name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ConfigError("backend names are letters, digits, dots, dashes")
    text = MODELS_YAML.read_text(encoding="utf-8")
    if name in set(yaml.safe_load(text).get("backends") or {}):
        raise ConfigError(f"backend {name!r} already exists")
    url = str((fields or {}).get("url") or "").strip()
    command = str((fields or {}).get("command") or "").strip()
    if not url or not command:
        raise ConfigError("a backend needs a url and a launch command")
    if "backends" not in (yaml.safe_load(text) or {}):
        # A file with no backends yet grows the section — the first
        # backend must not require a raw-editor detour.
        text = text.rstrip("\n") + "\n\nbackends:\n"
    try:
        text = yamledit.insert_block(text, ["backends"], name,
                                     {"url": url, "command": command})
    except yamledit.PathError as e:
        raise ConfigError(str(e)) from None
    config_store.write(MODELS_YAML, text, validate=validate_models_yaml)
    return {"restart": True}


def delete_backend(name: str) -> dict:
    text = MODELS_YAML.read_text(encoding="utf-8")
    if name not in set(yaml.safe_load(text).get("backends") or {}):
        raise ConfigError(f"no backend named {name!r}")
    try:
        text = yamledit.delete_block(text, ["backends"], name)
    except yamledit.PathError as e:
        raise ConfigError(str(e)) from None
    config_store.write(MODELS_YAML, text, validate=validate_models_yaml)
    return {"restart": True}
