"""The one validation idiom for everything structured.

Entity frontmatter, worker replies, and proposal files all validate
through this helper — no ad-hoc json.loads-plus-manual-checks anywhere
else. Returns human-readable issue strings instead of raising, because
every caller (validate_schema output, worker error surfaces, proposal
rejection notes) shows these strings to a human or a model.
"""

from __future__ import annotations

from jsonschema import Draft202012Validator


def validate(instance, schema: dict) -> list[str]:
    """Validate `instance` against a jsonschema. Empty list = conforms.

    Each issue reads "<path>: <problem>", e.g. "attendees: 3 is not of
    type 'array'" or "(root): 'id' is a required property".
    """
    validator = Draft202012Validator(schema)
    issues = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path)):
        path = ".".join(str(p) for p in err.absolute_path) or "(root)"
        issues.append(f"{path}: {err.message}")
    return issues
