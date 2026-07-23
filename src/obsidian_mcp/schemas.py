"""Entity schema validation (DESIGN-vault-schema.md).

config/schemas/<type>.yaml declares each entity type's required shape.
Ids are review watermarks: `no id` means never reviewed; `has id` means
reviewed once — NOT currently conforming, because the vault is
hand-edited and stamped entities can drift. So everything is always
re-checked; the watermark only prioritizes.
"""

from __future__ import annotations

import datetime as dt
import re

import yaml

from . import jsonval
from .markdown import find_section
from .tasks import Task
from .vaultconfig import get_config

# Line-level entity types: their yaml files describe a parsed task/card
# line, never a note's frontmatter.
LINE_TYPES = {"task", "deliverable", "card"}

# Deliverable assignee: a Dataview-style inline field on the task line.
FOR_RE = re.compile(r"\[for::\s*([^\]]+)\]")

_schemas: dict[str, dict] | None = None


def load_schemas() -> dict[str, dict]:
    global _schemas
    if _schemas is None:
        _schemas = {}
        d = get_config().schemas_dir
        if d.is_dir():
            for p in sorted(d.glob("*.yaml")):
                _schemas[p.stem] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return _schemas


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


def line_kind(entity_type: str) -> str:
    """Which line schema applies to checkbox lines inside a note of this
    type: cards on boards, deliverables in notes typed `deliverable`, tasks elsewhere."""
    if entity_type == "kanban":
        return "card"
    if entity_type == "deliverable":
        return "deliverable"
    return "task"


def validate_line(task: Task, entity_type: str) -> list[str]:
    kind = line_kind(entity_type)
    schema = load_schemas().get(kind)
    if not schema:
        return []
    inst = {"id": task.block_id, "text": task.text, "due": task.due, "priority": task.priority}
    if kind == "deliverable":
        m = FOR_RE.search(task.raw)
        inst["for"] = m.group(1).strip() if m else None
    return jsonval.validate(inst, schema)


def validate_research_body(body: str) -> list[str]:
    if find_section(body, "Sources") is None:
        return ["body: missing required '## Sources' section (ranked source links)"]
    return []
