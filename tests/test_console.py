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

from chickenskratch import config_store, console
from chickenskratch.config_store import ConfigError
from chickenskratch_vault.vault import Vault


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
        "---\npolicies: {capture: verified}\nn_max: 5\n---\n\n"
        "# Vault Spec\n\nProse the user owns.\n")
    return gitvault


# ---- what it refuses ----


def test_a_setting_the_engine_would_shrug_off_is_an_error_here(spec_vault):
    """`_spec_overrides` is deliberately forgiving — a bad autonomy keeps
    the old posture, an unknown processor is dropped — so a typo never
    breaks every pass. Correct for the engine, useless as feedback: you
    asked for something and nothing happened. The console says why."""
    for fields, expected in [
        ({"fidelity": "loose"}, "fidelity must be one of"),
        ({"n_max": 0}, "between 1 and 50"),
        ({"n_max": "lots"}, "whole number"),
        ({"disabled_processors": ["nonsense"]}, "unknown processor"),
        ({"done_archive": "../escape.md"}, "inside the vault"),
        ({"done_archive": "Tasks/Archive"}, "ending in .md"),
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


def test_every_processor_and_setting_explains_itself(spec_vault, models_yaml):
    """A console listing `spec_drift` and `n_max` as bare identifiers asks
    you to already know what they do. Descriptions live on the registry
    entry and beside the setting's own rules, so a new processor arrives
    explained — or arrives visibly unexplained, which this catches."""
    from chickenskratch import processors

    vault = console.model(str(spec_vault))["vault"]
    assert {p["name"] for p in vault["trustable"]} == {p.name for p in processors.REGISTRY}
    for row in vault["trustable"]:
        assert row["title"] and row["title"] != row["name"], row["name"]
        assert len(row["blurb"]) > 60, row["name"]
    for key in console.CHOICES | {"n_max": 0, "worker": 0} | dict.fromkeys(console.PATHS):
        assert console.FIELD_HELP.get(key), f"{key} has no explanation"


def test_trusting_the_commands_phase_is_not_offered(spec_vault, models_yaml):
    """//commands can be switched off, but "trusted to apply" is
    meaningless for it — it is intrinsically trusted and never queues
    anything, so auto_apply never reads its name. A checkbox that
    silently does nothing is worse than one that isn't there."""
    vault = console.model(str(spec_vault))["vault"]
    assert "inline_command" not in {p["name"] for p in vault["trustable"]}
    assert "inline_command" in {p["name"] for p in vault["disablable"]}
    # still a legal Spec.md value, so validation must keep accepting it
    assert console.validate_spec(
        {"disabled_processors": ["inline_command"]}, str(spec_vault))


def test_which_processors_a_check_can_vouch_for_is_reported(spec_vault, models_yaml):
    """Under gated autonomy, trusting a processor with no deterministic
    verifier does nothing at all. The surface has to say which those are,
    or the checkbox lies."""
    rows = {p["name"]: p for p in console.model(str(spec_vault))["vault"]["trustable"]}
    assert rows["capture"]["verified"] is True        # verify_record
    assert rows["schema_fix"]["verified"] is True     # verify_schema_fix
    assert rows["grouping"]["verified"] is False      # judgment: always queues
    assert rows["plan_today"]["verified"] is False


def test_the_read_model_separates_yours_from_inherited(spec_vault, models_yaml):
    """A value shown without saying whether it is set or defaulted invites
    you to 'change' something that was never written down."""
    vault = console.model(str(spec_vault))["vault"]
    # the RESOLVED levels — a partial map filled from the defaults —
    # never a raw map that means something else than what the engine does
    assert vault["values"]["policies"]["capture"] == "verified"
    assert vault["values"]["policies"]["grouping"] == "ask"
    assert "policies" in vault["declared"] and "n_max" in vault["declared"]
    assert "fidelity" not in vault["declared"]    # inherited from repo defaults


def test_a_broken_models_yaml_still_renders_the_console(spec_vault, models_yaml):
    models_yaml.write_text("default: [unclosed\n", encoding="utf-8")
    payload = console.model(str(spec_vault))
    assert payload["models"]["error"]             # says so, in place
    assert payload["models"]["text"]              # and hands you the text to fix
    assert payload["vault"]["values"]["policies"]["capture"] == "verified"  # the rest still works


def test_saving_policies_is_what_the_engine_reads_back(spec_vault, models_yaml):
    """The console writes the complete map; the next pass resolves it —
    one key, one resolver, no second knob to disagree with."""
    from chickenskratch import processors as procs
    from chickenskratch_vault.vaultconfig import get_config

    levels = {p.name: "ask" for p in procs.REGISTRY}
    console.save_spec(str(spec_vault), {"policies": {**levels, "capture": "full"}})

    fm = yaml.safe_load((spec_vault / "Harness/Spec.md").read_text().split("---")[1])
    assert fm["policies"]["capture"] == "full"
    cfg = get_config(str(spec_vault))
    assert cfg.policy_for("capture") == "full"
    assert cfg.policy_for("schema_fix") == "ask"   # the map, not the default


def test_policies_validation_rejects_junk_and_partial_maps(spec_vault, models_yaml):
    from chickenskratch import processors as procs

    levels = {p.name: "ask" for p in procs.REGISTRY}
    with pytest.raises(console.ConfigError, match="unknown processor"):
        console.validate_spec({"policies": {**levels, "nope": "ask"}}, str(spec_vault))
    with pytest.raises(console.ConfigError, match="must be one of"):
        console.validate_spec({"policies": {**levels, "capture": "sometimes"}},
                              str(spec_vault))
    partial = dict(levels)
    partial.pop("capture")
    with pytest.raises(console.ConfigError, match="must name every processor"):
        console.validate_spec({"policies": partial}, str(spec_vault))


# ---- writes ----


def test_saving_spec_is_one_revertible_snapshot_and_keeps_the_body(spec_vault, models_yaml):
    before = len(Vault(str(spec_vault)).history())
    result = console.save_spec(str(spec_vault), {"fidelity": "strict", "n_max": 9})
    assert set(result["changed"]) == {"fidelity", "n_max"}

    text = (spec_vault / "Harness/Spec.md").read_text()
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["fidelity"] == "strict" and fm["n_max"] == 9
    assert "Prose the user owns." in text        # the body is not ours to rewrite

    v = Vault(str(spec_vault))
    assert len(v.history()) > before             # snapshot-bracketed like any edit
    v.revert(v.history()[0]["sha"])
    assert "fidelity" not in yaml.safe_load(
        (spec_vault / "Harness/Spec.md").read_text().split("---")[1])


def test_configuring_a_vault_that_never_had_a_pass_seeds_the_spec(gitvault, models_yaml):
    """A fresh vault has no Harness/Spec.md until the engine's first pass
    writes one. Refusing to configure it until you have run a pass would
    be a rule nobody could guess, so the console seeds it from the same
    helper the engine uses — one home for what a new vault starts with."""
    assert not (gitvault / "Harness/Spec.md").exists()
    result = console.save_spec(str(gitvault), {"fidelity": "strict"})
    assert result["seeded"] is True and result["changed"] == ["fidelity"]

    text = (gitvault / "Harness/Spec.md").read_text()
    fm = yaml.safe_load(text.split("---")[1])
    assert fm["fidelity"] == "strict"
    assert "n_max" in fm and "folders" in fm     # the full seed, not one key
    assert fm["policies"]["capture"] == "verified"   # the posture, legible
    assert "# Vault Spec" in text                # …over the prose that explains it


def test_saving_spec_writes_nothing_when_nothing_changed(spec_vault, models_yaml):
    console.save_spec(str(spec_vault), {"fidelity": "strict"})
    before = len(Vault(str(spec_vault)).history())
    assert console.save_spec(str(spec_vault), {"fidelity": "strict"})["changed"] == []
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


# ---- the AI layer: workers, schemas, profile/backend structure ----


GOOD_WORKER = """\
description: test worker
inputs: [text]
prompt: |
  Do the thing with {text}.
schema:
  type: object
  required: [ok]
  properties:
    ok: {type: string}
"""


@pytest.fixture()
def ai_dirs(tmp_path, monkeypatch, models_yaml):
    """Isolated workers/ + schemas/ dirs wired through VAULT_CONFIG_PATH,
    with history redirected — the console must never touch the repo's
    own config from a test."""
    (tmp_path / "workers").mkdir()
    (tmp_path / "schemas").mkdir()
    (tmp_path / "vault.yaml").write_text(
        "workers_dir: workers\nschemas_dir: schemas\n", encoding="utf-8")
    monkeypatch.setenv("VAULT_CONFIG_PATH", str(tmp_path / "vault.yaml"))
    return tmp_path


def test_worker_save_validates_with_the_real_loader(ai_dirs):
    console.save_worker("greeter", text=GOOD_WORKER)
    assert (ai_dirs / "workers" / "greeter.yaml").read_text() == GOOD_WORKER
    with pytest.raises(ConfigError, match="undeclared placeholder"):
        console.save_worker("greeter", text=GOOD_WORKER.replace("{text}", "{typo}"))
    # the rejected write left the good file alone
    assert (ai_dirs / "workers" / "greeter.yaml").read_text() == GOOD_WORKER


def test_worker_form_save_renders_canonical_yaml(ai_dirs):
    console.save_worker("greeter", fields={
        "description": "says hi",
        "inputs": ["text"],
        "timeout_s": 30, "temperature": 0.2, "max_tokens": 400,
        "prompt": "Say hi to {text}.\n{hedges}\n",
        "hedges": "- politely\n",
        "schema": "type: object\nrequired: [ok]\nproperties:\n  ok: {type: string}\n",
    })
    from chickenskratch_vault import workers as wk
    spec = wk.get_worker("greeter")
    assert spec.description == "says hi"
    assert spec.hedges == "- politely\n"
    assert spec.timeout_s == 30 and spec.max_tokens == 400
    # round-trip: what the console reads back parses to the same spec
    row = next(r for r in console.model(str(ai_dirs))["workers"]["items"]
               if r["name"] == "greeter")
    assert row["prompt"] == spec.prompt and row["hedges"] == spec.hedges


def test_engine_workers_refuse_deletion_by_name(ai_dirs):
    with pytest.raises(ConfigError, match="engine worker"):
        console.delete_worker("extract_record")


def test_worker_delete_is_undoable_via_restore(ai_dirs):
    console.save_worker("greeter", text=GOOD_WORKER)
    console.delete_worker("greeter")
    assert not (ai_dirs / "workers" / "greeter.yaml").exists()
    stamp = config_store.history(ai_dirs / "workers" / "greeter.yaml")[0].stamp
    console.restore_worker("greeter", stamp)
    assert (ai_dirs / "workers" / "greeter.yaml").read_text() == GOOD_WORKER


def test_test_worker_runs_the_draft_not_the_file(ai_dirs, monkeypatch):
    """Try-before-commit: the panel sends the unsaved draft, and the
    reply (or the worker's own refusal) comes back as the result."""
    import json as _json
    from types import SimpleNamespace

    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=_json.dumps({"ok": "ran"})))])

    monkeypatch.setattr(
        console.wk, "new_client",
        lambda: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
            close=lambda: _noop()))

    async def _noop():
        return None

    out = console.test_worker("greeter", {"text": "sample"}, text=GOOD_WORKER)
    assert out["reply"] == {"ok": "ran"}
    assert "Do the thing with sample." in out["prompt"]
    # a worker-side failure IS the result, not an HTTP error
    async def bad(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"wrong": 1}'))])
    monkeypatch.setattr(
        console.wk, "new_client",
        lambda: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=bad)),
            close=lambda: _noop()))
    out = console.test_worker("greeter", {"text": "s"}, text=GOOD_WORKER)
    assert "schema-invalid" in out["error"]


def test_schema_save_and_the_form_round_trip(ai_dirs):
    console.save_schema("widget", fields={
        "description": "a widget", "id_prefix": "w",
        "fields": [
            {"name": "id", "kind": "string", "required": True, "nullable": False,
             "pattern": "^w-", "min_length": 0, "enum": [], "const": None},
            {"name": "status", "kind": "enum", "required": False, "nullable": True,
             "pattern": "", "min_length": 0, "enum": ["open", "done"], "const": None},
        ],
    })
    row = next(r for r in console.model(str(ai_dirs))["schemas"]["items"]
               if r["name"] == "widget")
    assert row["formable"] and row["id_prefix"] == "w"
    assert {f["name"] for f in row["form"]} == {"id", "status"}
    from chickenskratch_vault import schemas as vsch
    assert vsch.validate_frontmatter("widget", {"status": "open"}) != []  # id required


def test_engine_schemas_refuse_deletion_and_folder_referenced_refuse(ai_dirs, vault):
    with pytest.raises(ConfigError, match="engine schema"):
        console.delete_schema("proposal", str(vault))
    (vault / "Harness").mkdir(exist_ok=True)
    (vault / "Harness/Spec.md").write_text(
        "---\nfolders: {Meetings: meeting}\n---\n", encoding="utf-8")
    (ai_dirs / "schemas" / "meeting.yaml").write_text("type: object\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="folder"):
        console.delete_schema("meeting", str(vault))


def test_the_default_record_kind_cannot_be_deleted(ai_dirs, vault):
    (ai_dirs / "schemas" / "scrap.yaml").write_text(
        "record:\n  kind: scrap\n  default: true\n  cues: '- x'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="DEFAULT record kind"):
        console.delete_schema("scrap", str(vault))


def test_one_broken_schema_file_never_takes_down_the_console(ai_dirs):
    """The console is where you go to FIX a broken file — so a schema
    file with bad YAML must render as a broken row (raw text + reason),
    not 500 the whole model() through a worker's kind composition."""
    (ai_dirs / "schemas" / "capture.yaml").write_text(
        "record: {kind: capture, default: true, cues: '- x'}\n", encoding="utf-8")
    console.save_worker("rec", text=(
        "inputs: [text]\n"
        "prompt: |\n  {kinds}\n  {text}\n"
        "schema:\n  type: object\n  properties:\n    record:\n"
        "      type: object\n      properties:\n        kind: {enum: [x]}\n"))
    assert not next(r for r in console.model(str(ai_dirs))["workers"]["items"]
                    if r["name"] == "rec")["invalid"]
    (ai_dirs / "schemas" / "broken.yaml").write_text("description: [unclosed",
                                                     encoding="utf-8")
    model = console.model(str(ai_dirs))   # must not raise
    worker_row = next(r for r in model["workers"]["items"] if r["name"] == "rec")
    assert "broken.yaml" in worker_row["invalid"]
    schema_row = next(r for r in model["schemas"]["items"] if r["name"] == "broken")
    assert schema_row["invalid"] and schema_row["text"] == "description: [unclosed"


def test_a_deleted_file_still_offers_its_backups(ai_dirs):
    """The delete toast promises the backups remain restorable; the rows
    must keep that promise — a deleted worker appears as a deleted row
    carrying its backup list, and restoring it through the normal
    restore path brings the file back."""
    console.save_worker("greeter", text=GOOD_WORKER)
    console.delete_worker("greeter")
    row = next(r for r in console.model(str(ai_dirs))["workers"]["items"]
               if r["name"] == "greeter")
    assert row["deleted"] and row["file"]["backups"]
    console.restore_worker("greeter", row["file"]["backups"][0]["stamp"])
    assert (ai_dirs / "workers" / "greeter.yaml").read_text() == GOOD_WORKER
    # restored: back to a normal, editable row
    row = next(r for r in console.model(str(ai_dirs))["workers"]["items"]
               if r["name"] == "greeter")
    assert not row.get("deleted")


def test_a_numeric_enum_is_raw_editor_only(ai_dirs):
    """The form's enum input round-trips strings. Numbers would come
    back retyped as strings — a silent schema rewrite — so such a file
    is honest about being beyond the form."""
    (ai_dirs / "schemas" / "scored.yaml").write_text(
        "type: object\nproperties:\n  level:\n    enum: [1, 2, 3]\n",
        encoding="utf-8")
    row = next(r for r in console.model(str(ai_dirs))["schemas"]["items"]
               if r["name"] == "scored")
    assert not row["formable"]


def test_a_nullable_boolean_survives_the_form(ai_dirs):
    """The nullable checkbox must mean something for every kind that
    shows it — a boolean that silently dropped it saved a schema the
    user didn't ask for."""
    console.save_schema("flags", fields={
        "description": "", "id_prefix": "",
        "fields": [{"name": "archived", "kind": "boolean", "required": False,
                    "nullable": True, "pattern": "", "min_length": 0,
                    "enum": [], "const": None}],
    })
    row = next(r for r in console.model(str(ai_dirs))["schemas"]["items"]
               if r["name"] == "flags")
    assert row["formable"]
    field = next(f for f in row["form"] if f["name"] == "archived")
    assert field["kind"] == "boolean" and field["nullable"]
    parsed = yaml.safe_load((ai_dirs / "schemas" / "flags.yaml").read_text())
    assert parsed["properties"]["archived"]["type"] == ["boolean", "null"]


def test_a_second_default_kind_is_refused_at_save(ai_dirs):
    console.save_schema("one", text="record:\n  kind: one\n  default: true\n  cues: '- x'\n")
    with pytest.raises(ConfigError, match="exactly one record kind"):
        console.save_schema("two", text="record:\n  kind: two\n  default: true\n  cues: '- y'\n")


def test_profile_create_and_delete_preserve_every_other_byte(models_yaml):
    before = models_yaml.read_text()
    console.create_profile("homelab", {
        "model": "qwen3.5:72b", "base_url": "http://192.168.1.50:8000/v1",
        "worker_max_tokens": 8000})
    text = models_yaml.read_text()
    assert "# what chat talks to" in text          # comments survive
    parsed = yaml.safe_load(text)["models"]["homelab"]
    assert parsed == {"model": "qwen3.5:72b",
                      "base_url": "http://192.168.1.50:8000/v1",
                      "worker_max_tokens": 8000}
    console.delete_profile("homelab", "/nonexistent-vault")
    assert models_yaml.read_text() == before       # byte-identical round trip


def test_profiles_in_use_refuse_deletion(models_yaml):
    with pytest.raises(ConfigError, match="chat default"):
        console.delete_profile("big", "/nonexistent-vault")
    with pytest.raises(ConfigError, match="worker profile"):
        console.delete_profile("small", "/nonexistent-vault")


def test_api_key_env_takes_a_name_never_a_key(models_yaml):
    with pytest.raises(ConfigError, match="NAME of an environment variable"):
        console.create_profile("leaky", {
            "model": "m", "base_url": "http://x/v1",
            "api_key_env": "sk-abc123-the-actual-key"})


def test_backend_create_and_delete(models_yaml):
    console.create_backend("mlx-test", {
        "url": "http://127.0.0.1:9999/v1", "command": "echo serve"})
    assert yaml.safe_load(models_yaml.read_text())["backends"]["mlx-test"] == {
        "url": "http://127.0.0.1:9999/v1", "command": "echo serve"}
    console.delete_backend("mlx-test")
    assert "backends" not in (yaml.safe_load(models_yaml.read_text()) or {}) or \
        "mlx-test" not in (yaml.safe_load(models_yaml.read_text()).get("backends") or {})


def test_the_enlarged_model_still_never_carries_a_secret(
        spec_vault, models_yaml, ai_dirs, monkeypatch):
    monkeypatch.setenv("FAKE_KEY", "sk-secret-value")
    payload = json.dumps(console.model(str(spec_vault)))
    assert "sk-secret-value" not in payload
