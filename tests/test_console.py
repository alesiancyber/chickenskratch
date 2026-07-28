"""The management console: what it refuses, what it never shows, and how
a change is undone.

Two homes, two guarantees. Spec.md is a vault note, so a console edit is
an ordinary gated write — one snapshot, revertible from the changes feed.
config/models.yaml is not in the vault, so it gets the substitute
config_store provides: validate, keep the old bytes, restore on demand.
"""

import json

import pytest
import yaml

from tezcat import config_store, console
from tezcat.config_store import ConfigError
from tezcat_vault.vault import Vault


@pytest.fixture()
def models_yaml(tmp_path, monkeypatch):
    """A real models.yaml, with comments, pointed at by the console."""
    path = tmp_path / "models.yaml"
    path.write_text("""\
default: big      # what chat talks to
worker: small

models:
  big:
    model: reasoning-model
    base_url: https://example.invalid/v1
    api_key_env: FAKE_KEY
    worker_max_tokens: 16000
  small:
    model: tiny-model
    base_url: http://localhost:11434/v1
    worker_adapt: small
""", encoding="utf-8")
    monkeypatch.setattr(console, "MODELS_YAML", path)
    monkeypatch.setattr(config_store, "HISTORY_DIR", tmp_path / "history")
    return path


@pytest.fixture()
def spec_vault(gitvault):
    (gitvault / "Harness").mkdir(exist_ok=True)
    (gitvault / "Harness/Spec.md").write_text(
        "---\nautonomy: gated\nn_max: 5\n---\n\n# Vault Spec\n\nProse the user owns.\n")
    return gitvault


# ---- what it refuses ----


def test_a_setting_the_engine_would_shrug_off_is_an_error_here(spec_vault):
    """`_spec_overrides` is deliberately forgiving — a bad autonomy keeps
    the old posture, an unknown processor is dropped — so a typo never
    breaks every pass. Correct for the engine, useless as feedback: you
    asked for something and nothing happened. The console says why."""
    for fields, expected in [
        ({"autonomy": "totally"}, "autonomy must be one of"),
        ({"fidelity": "loose"}, "fidelity must be one of"),
        ({"n_max": 0}, "between 1 and 50"),
        ({"n_max": "lots"}, "whole number"),
        ({"auto_apply": ["capture", "nonsense"]}, "unknown processor"),
        ({"capture_inbox": "../escape.md"}, "inside the vault"),
        ({"capture_inbox": "Tasks/Inbox"}, "ending in .md"),
        ({"surfaces": {}}, "not a console-managed setting"),
    ]:
        with pytest.raises(ConfigError, match=expected):
            console.validate_spec(fields, str(spec_vault))


def test_a_worker_naming_no_profile_is_refused(spec_vault, models_yaml):
    with pytest.raises(ConfigError, match="no model profile named 'ghost'"):
        console.validate_spec({"worker": "ghost"}, str(spec_vault))
    assert console.validate_spec({"worker": "small"}, str(spec_vault)) == {"worker": "small"}
    assert console.validate_spec({"worker": ""}, str(spec_vault)) == {"worker": ""}


def test_models_yaml_that_would_not_load_is_refused(models_yaml):
    with pytest.raises(ConfigError, match="not valid YAML"):
        console.validate_models_yaml("default: [unclosed\n")
    with pytest.raises(ConfigError, match="chat would not start"):
        console.validate_models_yaml("default: ghost\nmodels:\n  big:\n    model: m\n")
    with pytest.raises(ConfigError, match="every draft would fail"):
        console.validate_models_yaml(
            "default: big\nworker: ghost\nmodels:\n  big:\n    model: m\n")


# ---- what it never shows ----


def test_a_key_is_reported_by_name_and_presence_never_by_value(
        spec_vault, models_yaml, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "sk-do-not-leak-me")
    payload = json.dumps(console.model(str(spec_vault)))
    assert "sk-do-not-leak-me" not in payload

    rows = {p["name"]: p for p in console.model(str(spec_vault))["models"]["profiles"]}
    assert rows["big"]["api_key_env"] == "FAKE_KEY" and rows["big"]["key_set"] is True
    monkeypatch.delenv("FAKE_KEY")
    rows = {p["name"]: p for p in console.model(str(spec_vault))["models"]["profiles"]}
    assert rows["big"]["key_set"] is False   # the surface can say "this will fail"


def test_the_read_model_separates_yours_from_inherited(spec_vault, models_yaml):
    """A value shown without saying whether it is set or defaulted invites
    you to 'change' something that was never written down."""
    vault = console.model(str(spec_vault))["vault"]
    assert vault["values"]["autonomy"] == "gated"
    assert "autonomy" in vault["declared"] and "n_max" in vault["declared"]
    assert "fidelity" not in vault["declared"]    # inherited from repo defaults


def test_a_broken_models_yaml_still_renders_the_console(spec_vault, models_yaml):
    models_yaml.write_text("default: [unclosed\n", encoding="utf-8")
    payload = console.model(str(spec_vault))
    assert payload["models"]["error"]             # says so, in place
    assert payload["models"]["text"]              # and hands you the text to fix
    assert payload["vault"]["values"]["autonomy"] == "gated"   # the rest still works


# ---- writes ----


def test_saving_spec_is_one_revertible_snapshot_and_keeps_the_body(spec_vault, models_yaml):
    before = len(Vault(str(spec_vault)).history())
    result = console.save_spec(str(spec_vault), {"autonomy": "full", "n_max": 9})
    assert set(result["changed"]) == {"autonomy", "n_max"}

    text = (spec_vault / "Harness/Spec.md").read_text()
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["autonomy"] == "full" and fm["n_max"] == 9
    assert "Prose the user owns." in text        # the body is not ours to rewrite

    v = Vault(str(spec_vault))
    assert len(v.history()) > before             # snapshot-bracketed like any edit
    v.revert(v.history()[0]["sha"])
    assert yaml.safe_load(
        (spec_vault / "Harness/Spec.md").read_text().split("---")[1])["autonomy"] == "gated"


def test_configuring_a_vault_that_never_had_a_pass_seeds_the_spec(gitvault, models_yaml):
    """A fresh vault has no Harness/Spec.md until the engine's first pass
    writes one. Refusing to configure it until you have run a pass would
    be a rule nobody could guess, so the console seeds it from the same
    helper the engine uses — one home for what a new vault starts with."""
    assert not (gitvault / "Harness/Spec.md").exists()
    result = console.save_spec(str(gitvault), {"autonomy": "full"})
    assert result["seeded"] is True and result["changed"] == ["autonomy"]

    text = (gitvault / "Harness/Spec.md").read_text()
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["autonomy"] == "full"
    assert "n_max" in fm and "folders" in fm     # the full seed, not one key
    assert "# Vault Spec" in text                # …over the prose that explains it


def test_saving_spec_writes_nothing_when_nothing_changed(spec_vault, models_yaml):
    console.save_spec(str(spec_vault), {"autonomy": "full"})
    before = len(Vault(str(spec_vault)).history())
    assert console.save_spec(str(spec_vault), {"autonomy": "full"})["changed"] == []
    assert len(Vault(str(spec_vault)).history()) == before   # no empty commit


def test_model_fields_are_set_surgically_and_backed_up(models_yaml):
    original = models_yaml.read_text()
    console.save_model_fields({"worker": "big",
                               "profiles": {"small": {"worker_adapt": "none"}}})
    data = yaml.safe_load(models_yaml.read_text())
    assert data["worker"] == "big"
    assert data["models"]["small"]["worker_adapt"] == "none"
    assert "# what chat talks to" in models_yaml.read_text()   # comments survive

    (backup,) = config_store.history(models_yaml)
    assert backup.path.read_text() == original


def test_a_rejected_write_leaves_no_trace(models_yaml):
    """Validation runs BEFORE the backup: a refused write that still
    snapshotted would fill the history with versions of a file nothing
    ever changed."""
    original = models_yaml.read_text()
    with pytest.raises(ConfigError):
        console.save_models_text("default: [unclosed\n")
    assert models_yaml.read_text() == original
    assert config_store.history(models_yaml) == []


def test_restore_puts_it_back_and_is_itself_undoable(models_yaml):
    original = models_yaml.read_text()
    console.save_model_fields({"worker": "big"})
    stamp = config_store.history(models_yaml)[0].stamp

    console.restore_models(stamp)
    assert models_yaml.read_text() == original
    # the restore snapshotted what it replaced, so the "wrong" version is
    # still reachable — the one action whose purpose is recovering from a
    # mistake must not be the one you cannot undo
    assert len(config_store.history(models_yaml)) == 2
    assert any(yaml.safe_load(b.path.read_text())["worker"] == "big"
               for b in config_store.history(models_yaml))


def test_restart_needed_is_reported_honestly(models_yaml):
    """The worker profile is re-resolved every pass; the chat default and
    anything a running agent already holds are not. A setting that looks
    broken because it simply is not loaded yet is the worst kind."""
    assert console.save_model_fields({"worker": "big"})["restart"] is False
    assert console.save_model_fields(
        {"profiles": {"small": {"worker_adapt": "none"}}})["restart"] is False
    assert console.save_model_fields({"default": "small"})["restart"] is True
    assert console.save_model_fields(
        {"profiles": {"small": {"max_iterations": 3}}})["restart"] is True


def test_an_unknown_profile_or_field_is_refused(models_yaml):
    with pytest.raises(ConfigError, match="no model profile named 'ghost'"):
        console.save_model_fields({"worker": "ghost"})
    with pytest.raises(ConfigError, match="not a console-managed profile field"):
        console.save_model_fields({"profiles": {"small": {"base_url": "http://evil"}}})
    with pytest.raises(ConfigError, match="worker_adapt must be"):
        console.save_model_fields({"profiles": {"small": {"worker_adapt": "medium"}}})
