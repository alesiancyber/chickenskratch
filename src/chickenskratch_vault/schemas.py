"""Entity schema validation and record-kind metadata.

config/schemas/<type>.yaml declares each entity type's required shape.
Ids are review watermarks: `no id` means never reviewed; `has id` means
reviewed once — NOT currently conforming, because the vault is
hand-edited and stamped entities can drift. So everything is always
re-checked; the watermark only prioritizes.

Besides the jsonschema keys, a file may carry metadata the validator
never sees: `description:` (console prose), `id_prefix:` (stamp prefix
for this type), and `record:` (a capture kind — how extract_record
detects and files dumps of this shape). Metadata is split off at load,
so a metadata-only file (capture.yaml) enforces nothing by construction.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import string

import yaml

from . import jsonval
from .markdown import find_section
from .tasks import Task
from .vault import VaultError
from .vaultconfig import VaultConfig, get_config

# Line-level entity types: their yaml files describe a parsed task/card
# line, never a note's frontmatter.
LINE_TYPES = {"task", "card"}

# One name grammar for everything config-authored that becomes a file
# stem, a kind name, or a worker name — workers.py and the console both
# validate against this, so the rule can't fork.
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Keys split off before a file's remainder is used as a jsonschema.
_METADATA_KEYS = ("description", "id_prefix", "record")


def split_metadata(doc: dict) -> tuple[dict, dict]:
    """(metadata, schema-remainder) of one schema document. The single
    split — the loader, the console's rows, and the console's save
    validation all call this, so no copy can disagree about which keys
    the validator never sees. Does not mutate `doc`."""
    doc = dict(doc)
    meta = {k: doc.pop(k) for k in _METADATA_KEYS if k in doc}
    return meta, doc


def template_placeholders(template: str) -> set[str]:
    """The {slots} a format template names. Shared by build_worker's
    placeholder/inputs contract and parse_record_block's cue check —
    config-authored text headed for .format() is validated here or it
    reaches the format pass unchecked."""
    try:
        return {field for _, field, _, _ in string.Formatter().parse(template)
                if field is not None}
    except ValueError as e:
        raise VaultError(f"bad placeholder syntax: {e}") from None

# (dir snapshot, {stem: schema}, {stem: metadata}) — rebuilt whenever any
# file in the dir appears, vanishes, or changes mtime. Assigned only
# after a full successful parse, so a broken file never poisons the
# cache with a partial read (it raises, and the old entry survives).
_cache: tuple[tuple, dict[str, dict], dict[str, dict]] | None = None


def _dir_snapshot(d) -> tuple:
    entries = []
    for p in d.glob("*.yaml"):
        try:
            entries.append((p.name, p.stat().st_mtime))
        except OSError:
            continue  # deleted between glob and stat: absent either way
    return (str(d), tuple(sorted(entries)))


def _load() -> tuple[dict[str, dict], dict[str, dict]]:
    global _cache
    d = get_config().schemas_dir
    snap = _dir_snapshot(d) if d.is_dir() else (str(d), ())
    if _cache is not None and _cache[0] == snap:
        return _cache[1], _cache[2]
    schemas: dict[str, dict] = {}
    meta: dict[str, dict] = {}
    if d.is_dir():
        for p in sorted(d.glob("*.yaml")):
            # Every parse failure leaves here as VaultError naming the
            # file: these load mid-pass and mid-request, and a raw
            # YAMLError from a hand-edit escapes every caller's error
            # contract at once (the engine's, and the console's — the
            # one surface you'd use to fix the file).
            try:
                doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as e:
                raise VaultError(f"{p.name}: not valid YAML: {e}") from None
            if not isinstance(doc, dict):
                raise VaultError(f"{p.name}: expected a mapping at top level")
            meta[p.stem], schemas[p.stem] = split_metadata(doc)
    _cache = (snap, schemas, meta)
    return schemas, meta


def load_schemas() -> dict[str, dict]:
    return _load()[0]


def _jsonable(value):
    """Frontmatter YAML parses `date: 2026-07-17` into datetime.date —
    normalize to ISO strings so jsonschema sees what the file says."""
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def validate_frontmatter(entity_type: str, fm: dict) -> list[str]:
    """Human-readable issues; empty = conforms. Types without a declared
    note schema (daily, unmapped folders, line types) have nothing to
    enforce."""
    if entity_type in LINE_TYPES:
        return []
    schema = load_schemas().get(entity_type)
    if not schema:
        return []
    return jsonval.validate(_jsonable(fm), schema)


# VaultConfig attrs a record kind's dir_key may point at — the settings
# whose value is a folder notes can file into. THE validator for
# dir_key: the console offers exactly this list and parse_record_block
# enforces it, so a hand-edited file can't name `exclude` and file notes
# into str(tuple). Guarded against VaultConfig drift at import.
DIR_KEYS = ("meetings_dir", "daily_dir", "capture_archive")
assert set(DIR_KEYS) <= {f.name for f in dataclasses.fields(VaultConfig)}

# The placeholders cue/guidance text may carry into a composed prompt:
# the date anchors every extraction call fills. Checked here, at the
# block's single validator, because cues splice into a prompt BEFORE its
# format pass — an unknown {slot} surviving to .format() is a KeyError
# outside the VaultError contract, crashing every pass at draft time
# instead of refusing the file at save time.
_CUE_SLOTS = frozenset({"today", "this_sunday", "next_sunday"})


@dataclasses.dataclass(frozen=True)
class RecordKind:
    """One capture kind: how extract_record recognizes a dump of this
    shape and where the drafted record files. Declared in the entity's
    schema file (`record:` block) so a new kind is one config file."""

    name: str            # the reply-enum value ("meeting")
    cues: str            # prompt bullet(s): when this kind applies
    guidance: str = ""   # extra bullets (e.g. the transcript EXCEPTION)
    files_as: str = "daily-append"   # or "note": its own note per record
    dir_key: str = ""    # VaultConfig attr naming the folder, files_as: note
    default: bool = False


def parse_record_block(stem: str, block) -> RecordKind:
    """Validate ONE file's record block. Shared by record_kinds and the
    console's save path, so a block that saves is a block that loads."""
    if not isinstance(block, dict):
        raise VaultError(f"{stem}.yaml: record: must be a mapping")
    known = {f.name for f in dataclasses.fields(RecordKind)} - {"name"}
    unknown = sorted(set(block) - known - {"kind"})
    if unknown:
        raise VaultError(f"{stem}.yaml: record: unknown key(s) {unknown}")
    name = str(block.get("kind") or stem)
    if not NAME_RE.match(name):
        raise VaultError(f"{stem}.yaml: record kind {name!r} must match {NAME_RE.pattern}")
    cues = str(block.get("cues") or "").strip()
    if not cues:
        raise VaultError(f"{stem}.yaml: record: needs cues (when does this kind apply?)")
    guidance = str(block.get("guidance") or "").strip()
    stray = sorted(template_placeholders(cues + "\n" + guidance) - _CUE_SLOTS)
    if stray:
        raise VaultError(
            f"{stem}.yaml: record cues/guidance use placeholder(s) {stray} — only "
            f"{sorted(_CUE_SLOTS)} are filled; write literal braces as {{{{ and }}}}")
    files_as = str(block.get("files_as") or "daily-append")
    if files_as not in ("note", "daily-append"):
        raise VaultError(f"{stem}.yaml: record files_as must be 'note' or 'daily-append'")
    dir_key = str(block.get("dir_key") or "")
    if files_as == "note" and dir_key not in DIR_KEYS:
        raise VaultError(
            f"{stem}.yaml: record dir_key must be one of {list(DIR_KEYS)}, got {dir_key!r}")
    return RecordKind(
        name=name, cues=cues, guidance=guidance,
        files_as=files_as, dir_key=dir_key, default=bool(block.get("default", False)),
    )


def kinds_default_issue(kinds) -> str | None:
    """The cross-kind invariant, stated once: a nonempty kind set names
    exactly one default. Both the loader and the console's save-time
    simulation enforce it through this — two copies of "exactly one"
    would drift into two opinions of which save is legal."""
    kinds = list(kinds)
    defaults = sorted(k.name for k in kinds if k.default)
    if kinds and len(defaults) != 1:
        return (f"record kinds declare {len(defaults)} defaults ({defaults}) — "
                "exactly one record kind must have default: true")
    return None


def record_kinds() -> dict[str, RecordKind]:
    """The declared capture kinds, validated. Raises VaultError naming
    the offending file — these blocks enter the tree through the
    console's validated write, so loudness here is the backstop."""
    kinds: dict[str, RecordKind] = {}
    for stem, m in sorted(_load()[1].items()):
        block = m.get("record")
        if block is None:
            continue
        kind = parse_record_block(stem, block)
        if kind.name in kinds:
            raise VaultError(f"{stem}.yaml: record kind {kind.name!r} declared twice")
        kinds[kind.name] = kind
    issue = kinds_default_issue(kinds.values())
    if issue:
        raise VaultError(issue)
    return kinds


def default_record_kind() -> str:
    """The kind extract_record falls back to when the model omits one."""
    kinds = record_kinds()
    for k in kinds.values():
        if k.default:
            return k.name
    return "capture"  # no kinds declared at all: the historical fallback


def id_prefix(entity_type: str) -> str:
    """Stamp prefix for this entity type's ids, from the schema file's
    `id_prefix:` metadata. Default "n" (a plain note)."""
    m = _load()[1].get(entity_type) or {}
    return str(m.get("id_prefix") or "n")


def line_kind(entity_type: str) -> str:
    """Which line schema applies to checkbox lines inside a note of this
    type: cards on boards, tasks everywhere else.

    `deliverable` used to be the third answer — a note typed that way had
    its lines held to a schema requiring a [for:: Name] inline field and a
    hard due date. Owed-ness is the 👤 task field now, and nothing has
    written [for::] since; the schema file said of itself that it should
    be deleted when its enforcement stopped mattering, and it had. A note
    still declaring `type: deliverable` keeps that type (frontmatter is
    always the note's own word) and simply validates as a task."""
    if entity_type == "kanban":
        return "card"
    return "task"


def validate_line(task: Task, entity_type: str) -> list[str]:
    schema = load_schemas().get(line_kind(entity_type))
    if not schema:
        return []
    return jsonval.validate(
        {"id": task.block_id, "text": task.text,
         "due": task.due, "priority": task.priority},
        schema)


def validate_research_body(body: str) -> list[str]:
    if find_section(body, "Sources") is None:
        return ["body: missing required '## Sources' section (ranked source links)"]
    return []
