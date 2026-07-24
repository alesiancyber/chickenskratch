"""The one validation idiom for everything structured.

Entity frontmatter, worker replies, and proposal files all validate
through this helper — no ad-hoc json.loads-plus-manual-checks anywhere
else. Returns human-readable issue strings instead of raising, because
every caller (validate_schema output, worker error surfaces, proposal
rejection notes) shows these strings to a human or a model.
"""

from __future__ import annotations

import json
from functools import lru_cache

from jsonschema import Draft202012Validator


@lru_cache(maxsize=64)
def _validator(schema_json: str) -> Draft202012Validator:
    # Keyed by canonical JSON, not id(): schema dicts are long-lived
    # (load_schemas cache, WORKERS constants) but a recycled id must
    # never resolve to the wrong validator. validate_schema runs this
    # over every note each engine pass, so construction is worth caching.
    return Draft202012Validator(json.loads(schema_json))


def validate(instance, schema: dict) -> list[str]:
    """Validate `instance` against a jsonschema. Empty list = conforms.

    Each issue reads "<path>: <problem>", e.g. "attendees: 3 is not of
    type 'array'" or "(root): 'id' is a required property".
    """
    validator = _validator(json.dumps(schema, sort_keys=True))
    issues = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path)):
        path = ".".join(str(p) for p in err.absolute_path) or "(root)"
        issues.append(f"{path}: {err.message}")
    return issues
