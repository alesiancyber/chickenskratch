"""Surgical scalar edits: the value changes, nothing else does.

config/models.yaml is more comment than config, and those comments carry
the reasoning behind the settings. A `safe_dump` round-trip would delete
all of it, so the console edits text in place — and the property worth
pinning is exactly that: byte-identical everywhere except the one value.
"""

import yaml

from chickenskratch.yamledit import PathError, set_scalar

SAMPLE = """\
# top matter
default: big          # the chat profile
worker: big

models:
  big:
    model: reasoning-model
    # why this is high
    max_iterations: 25
    confirm_writes: true      # capability doesn't waive the gate

  small:
    model: tiny
    worker_adapt: small
"""


def edited(path, value, text=SAMPLE):
    return set_scalar(text, path, value)


def test_a_scalar_change_touches_exactly_one_line():
    out = edited(["worker"], "small")
    before, after = SAMPLE.splitlines(), out.splitlines()
    assert len(before) == len(after)
    differing = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert len(differing) == 1
    assert yaml.safe_load(out)["worker"] == "small"


def test_a_trailing_comment_survives_and_stays_a_comment():
    """The bug this test exists for: dropping the whitespace before `#`
    makes it part of the VALUE, so `default: small# the chat profile`
    parses as one long string and the setting is silently wrong."""
    out = edited(["default"], "small")
    assert yaml.safe_load(out)["default"] == "small"
    assert "# the chat profile" in out
    assert out.count("#") == SAMPLE.count("#")


def test_nested_fields_resolve_by_indentation():
    out = edited(["models", "small", "worker_adapt"], "none")
    data = yaml.safe_load(out)
    assert data["models"]["small"]["worker_adapt"] == "none"
    assert data["models"]["big"]["model"] == "reasoning-model"   # sibling untouched
    assert "# why this is high" in out


def test_types_round_trip_as_yaml_not_as_strings():
    data = yaml.safe_load(edited(["models", "big", "confirm_writes"], False))
    assert data["models"]["big"]["confirm_writes"] is False
    data = yaml.safe_load(edited(["models", "big", "max_iterations"], 40))
    assert data["models"]["big"]["max_iterations"] == 40
    # a value that would otherwise read back as a bool/number has to quote
    data = yaml.safe_load(edited(["worker"], "yes"))
    assert data["worker"] == "yes"
    data = yaml.safe_load(edited(["worker"], ""))
    assert data["worker"] == ""


def test_a_missing_leaf_is_added_inside_its_block():
    out = edited(["models", "small", "worker_max_tokens"], 16000)
    data = yaml.safe_load(out)
    assert data["models"]["small"]["worker_max_tokens"] == 16000
    assert data["models"]["big"].get("worker_max_tokens") is None
    # inside the block, not dangling at column 0
    line = next(ln for ln in out.splitlines() if "worker_max_tokens" in ln)
    assert line.startswith("    ")


def test_a_missing_parent_is_an_error_not_an_invention():
    """Adding a field to a profile is a scalar assignment; conjuring the
    profile is a structural change, and doing it silently would leave a
    half-built entry the loader then rejects."""
    try:
        edited(["models", "nonexistent", "model"], "x")
    except PathError:
        pass
    else:
        raise AssertionError("expected PathError")


def test_a_hash_inside_a_quoted_value_is_not_a_comment():
    text = 'base_url: "http://x/v1#frag"\n'
    out = set_scalar(text, ["base_url"], "http://y/v1")
    assert yaml.safe_load(out)["base_url"] == "http://y/v1"
