"""Worker specs as config files: the loader's refusals, the mtime-keyed
hot reload, and the record-kind composition that makes extract_record's
kind list config-driven.

The repo's own config/workers/ + config/schemas/ are the migration
fixture: the engine workers must load from them exactly as the old
Python constants did, and the composed extract_record prompt must carry
the schema files' cues verbatim.
"""

from __future__ import annotations

import asyncio
import json
import os
import textwrap
from types import SimpleNamespace

import pytest

from chickenskratch_vault import schemas, workers
from chickenskratch_vault.vault import VaultError

GOOD = textwrap.dedent("""\
    description: test worker
    inputs: [text, today]
    timeout_s: 30
    temperature: 0.3
    max_tokens: 500
    prompt: |
      Do the thing with {text} on {today}.
      {hedges}
    hedges: |
      - be careful
    schema:
      type: object
      required: [ok]
      properties:
        ok: {type: string}
    """)


@pytest.fixture()
def workers_dir(tmp_path, monkeypatch):
    """An isolated workers/schemas pair wired through VAULT_CONFIG_PATH,
    the same seam the MCP server uses."""
    (tmp_path / "workers").mkdir()
    (tmp_path / "schemas").mkdir()
    (tmp_path / "vault.yaml").write_text(
        "workers_dir: workers\nschemas_dir: schemas\n", encoding="utf-8")
    monkeypatch.setenv("VAULT_CONFIG_PATH", str(tmp_path / "vault.yaml"))
    return tmp_path / "workers"


def bump_mtime(path):
    """Force a distinct mtime — same-second writes must still invalidate."""
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 2))


# ---- build_worker: the single validator ----


@pytest.mark.parametrize("mutation, expected", [
    ("no_prompt", "needs a prompt"),
    ("no_schema", "needs a reply schema"),
    ("bad_schema", "not valid jsonschema"),
    ("unknown_key", "unknown key"),
    ("hedges_no_slot", "no {hedges} slot"),
    ("extra_placeholder", "undeclared placeholder"),
    ("unused_input", "never appear in the prompt"),
    ("bad_number", "must be between"),
    ("not_yaml", "not valid YAML"),
    ("not_mapping", "mapping at top level"),
])
def test_a_file_that_would_not_run_is_refused_with_the_reason(mutation, expected):
    text = {
        "no_prompt": GOOD.replace("prompt: |", "not_prompt_x: |").replace(
            "not_prompt_x", "description2") if False else
            GOOD.replace("prompt: |\n  Do the thing with {text} on {today}.\n  {hedges}\n", ""),
        "no_schema": GOOD[:GOOD.index("schema:")],
        "bad_schema": GOOD.replace("type: object", "type: 17"),
        "unknown_key": GOOD + "surprise: true\n",
        "hedges_no_slot": GOOD.replace("  {hedges}\n", ""),
        "extra_placeholder": GOOD.replace("{today}", "{tomorrow}"),
        "unused_input": GOOD.replace("inputs: [text, today]", "inputs: [text, today, extra]"),
        "bad_number": GOOD.replace("temperature: 0.3", "temperature: 9"),
        "not_yaml": "prompt: [unclosed\n",
        "not_mapping": "- just\n- a list\n",
    }[mutation]
    with pytest.raises(VaultError, match=expected):
        workers.build_worker("testw", text)


def test_a_bad_name_is_refused_before_it_can_name_a_path():
    for name in ("../escape", "UPPER", "has space", ""):
        with pytest.raises(VaultError, match="must match"):
            workers.build_worker(name, GOOD)


def test_hedges_may_only_use_declared_placeholders():
    text = GOOD.replace("- be careful", "- be careful about {mystery}")
    with pytest.raises(VaultError, match="hedges use undeclared"):
        workers.build_worker("testw", text)


# ---- the loader: fresh per call, never poisoned ----


def test_an_edit_is_live_the_next_call_no_restart(workers_dir):
    path = workers_dir / "testw.yaml"
    path.write_text(GOOD, encoding="utf-8")
    assert "Do the thing" in workers.get_worker("testw").prompt
    path.write_text(GOOD.replace("Do the thing", "Do it DIFFERENTLY"), encoding="utf-8")
    bump_mtime(path)
    assert "Do it DIFFERENTLY" in workers.get_worker("testw").prompt


def test_a_broken_save_raises_and_never_poisons_the_cache(workers_dir):
    path = workers_dir / "testw.yaml"
    path.write_text(GOOD, encoding="utf-8")
    workers.get_worker("testw")
    path.write_text("prompt: [unclosed\n", encoding="utf-8")
    bump_mtime(path)
    with pytest.raises(VaultError, match="not valid YAML"):
        workers.get_worker("testw")
    path.write_text(GOOD.replace("Do the thing", "Fixed now"), encoding="utf-8")
    bump_mtime(path)
    assert "Fixed now" in workers.get_worker("testw").prompt


def test_a_missing_worker_is_a_clean_error_naming_the_path(workers_dir):
    with pytest.raises(VaultError, match="no worker 'ghost'"):
        workers.get_worker("ghost")


def test_a_missing_input_is_a_vaulterror_not_a_keyerror(workers_dir):
    """The comment in render_prompt's caller fears exactly this: a caller
    drifting from the file's inputs must fail inside run_worker's
    VaultError contract, not crash every pass with a KeyError."""
    path = workers_dir / "testw.yaml"
    path.write_text(GOOD, encoding="utf-8")
    spec = workers.get_worker("testw")
    with pytest.raises(VaultError, match=r"missing input\(s\) \['today'\]"):
        asyncio.run(workers.run_worker(spec, text="x"))


# ---- migration integrity: the repo's own files ----


def test_every_engine_worker_loads_from_the_repo_config():
    names = workers.worker_names()
    assert set(workers.RESERVED_WORKERS) <= set(names)
    for name in names:
        spec = workers.get_worker(name)
        assert spec.prompt and spec.schema


def test_engine_workers_declare_the_inputs_their_callers_pass():
    """The calling code compiles against these — watcher drafters,
    query.ask, the wrappers. A drift here is the missing-input error at
    every pass."""
    expected = {
        "answer": {"question", "notes"},
        "schema_fix": {"note", "entity", "issues", "content", "new_id", "extra_ids"},
        "extract_tasks": {"text", "today", "this_sunday", "next_sunday"},
        "extract_record": {"text", "today", "this_sunday", "next_sunday"},
        "plan_today": {"today", "max_picks", "committed", "tasks"},
        "overlap": {"pairs"},
        "grouping": {"tasks"},
    }
    for name, inputs in expected.items():
        assert set(workers.get_worker(name).inputs) == inputs, name


def test_extract_record_composes_from_the_schema_files():
    """The kind list is generated: the meeting cues, the transcript
    EXCEPTION, and the capture fallback ride in from config/schemas/*,
    and the reply enum matches. Adding a kind is one schema file."""
    spec = workers.get_worker("extract_record")
    assert "{kinds}" not in spec.prompt and "{kind_choices}" not in spec.prompt
    assert 'kind "meeting" ONLY when the dump clearly recounts' in spec.prompt
    assert "EXCEPTION — a dump of speaker-labeled turns" in spec.prompt
    assert 'is kind "capture"' in spec.prompt
    kind_schema = spec.schema["properties"]["record"]["properties"]["kind"]
    assert kind_schema["enum"] == ["meeting", "capture"]
    assert '"kind": "meeting or capture"' in spec.prompt


def test_a_new_schema_file_extends_the_composed_prompt(workers_dir):
    root = workers_dir.parent
    (workers_dir / "rec.yaml").write_text(textwrap.dedent("""\
        inputs: [text]
        prompt: |
          The record:
          {kinds}
          Shape: {{"record": {{"kind": "{kind_choices}"}}}}
          Dump:
          {text}
        schema:
          type: object
          properties:
            record:
              type: object
              properties:
                kind: {enum: [placeholder]}
        """), encoding="utf-8")
    (root / "schemas" / "capture.yaml").write_text(textwrap.dedent("""\
        record:
          kind: capture
          default: true
          cues: |
            - Everything else is kind "capture".
        """), encoding="utf-8")
    (root / "schemas" / "standup.yaml").write_text(textwrap.dedent("""\
        record:
          kind: standup
          files_as: note
          dir_key: meetings_dir
          cues: |
            - kind "standup" when the dump is a per-person status round-up.
        """), encoding="utf-8")
    spec = workers.get_worker("rec")
    assert 'kind "standup" when the dump' in spec.prompt
    assert '"kind": "standup or capture"' in spec.prompt
    assert spec.schema["properties"]["record"]["properties"]["kind"]["enum"] == \
        ["standup", "capture"]
    # and the schema edit is live without touching the worker file
    (root / "schemas" / "standup.yaml").write_text(textwrap.dedent("""\
        record:
          kind: standup
          files_as: note
          dir_key: meetings_dir
          cues: |
            - kind "standup" when people give ROUND-ROBIN status.
        """), encoding="utf-8")
    bump_mtime(root / "schemas" / "standup.yaml")
    assert "ROUND-ROBIN" in workers.get_worker("rec").prompt


def test_a_kinds_prompt_with_no_declared_kinds_fails_loud(workers_dir):
    (workers_dir / "rec.yaml").write_text(
        "inputs: [text]\n"
        "prompt: |\n  {kinds}\n  {text}\n"
        "schema:\n  type: object\n  properties:\n    record:\n"
        "      type: object\n      properties:\n        kind: {enum: [x]}\n",
        encoding="utf-8")
    with pytest.raises(VaultError, match="no schema file declares"):
        workers.get_worker("rec")


def test_run_worker_reports_attempts_in_stats(workers_dir):
    """The silent retry, made visible: a model that needs the second
    attempt looks identical to one that doesn't in every reply — stats
    is how the eval runner sees the difference (marginal schema
    discipline, double latency)."""
    path = workers_dir / "testw.yaml"
    path.write_text(GOOD, encoding="utf-8")
    replies = iter(["this is not json", json.dumps({"ok": "yes"})])

    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=next(replies)))])

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    spec = workers.get_worker("testw")
    stats: dict = {}
    out = asyncio.run(workers.run_worker(spec, client=client, stats=stats,
                                         text="a", today="b"))
    assert out == {"ok": "yes"} and stats["attempts"] == 2


def test_run_worker_takes_an_explicit_client(workers_dir):
    """The console's test-run seam: a caller-supplied client is used, the
    module singleton untouched — it is bound to another thread's loop."""
    path = workers_dir / "testw.yaml"
    path.write_text(GOOD, encoding="utf-8")

    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps({"ok": "yes"})))])

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    spec = workers.get_worker("testw")
    out = asyncio.run(workers.run_worker(spec, client=client, text="a", today="b"))
    assert out == {"ok": "yes"}


# ---- schema metadata accessors ----


def test_id_prefix_comes_from_the_schema_files():
    assert schemas.id_prefix("meeting") == "m"
    assert schemas.id_prefix("research") == "r"
    assert schemas.id_prefix("kanban") == "b"
    assert schemas.id_prefix("unheard_of") == "n"


def test_a_broken_schema_file_is_a_vaulterror_not_a_yaml_crash(workers_dir):
    """These files reload mid-pass and mid-request on every mtime move.
    A hand-edit that breaks the YAML must surface as VaultError — the
    contract every caller (the engine's processors, the console) catches
    — never as a raw ParserError that 500s the console, the one surface
    you'd use to fix the file."""
    root = workers_dir.parent
    (root / "schemas" / "broken.yaml").write_text("description: [unclosed",
                                                  encoding="utf-8")
    with pytest.raises(VaultError, match="broken.yaml: not valid YAML"):
        schemas.record_kinds()
    with pytest.raises(VaultError, match="broken.yaml"):
        schemas.validate_frontmatter("meeting", {})


def test_braces_in_cues_are_refused_not_a_2am_keyerror(workers_dir):
    """Cues splice into a prompt BEFORE its format pass. An unknown
    {slot} in them used to survive to .format() and crash every
    extract_record call with a KeyError at draft time; the block's
    single validator refuses it at save/load instead. The date anchors
    stay allowed — hedges enjoy the same privilege."""
    root = workers_dir.parent
    (root / "schemas" / "memo.yaml").write_text(textwrap.dedent("""\
        record:
          kind: memo
          default: true
          cues: |
            - kind "memo" when it looks {like this}.
        """), encoding="utf-8")
    with pytest.raises(VaultError, match=r"memo.yaml.*placeholder.*like this"):
        schemas.record_kinds()
    # the anchors are legitimate cue placeholders, and escaped braces are literal
    (root / "schemas" / "memo.yaml").write_text(textwrap.dedent("""\
        record:
          kind: memo
          default: true
          cues: |
            - kind "memo" for notes dated {today}, shaped {{"x": 1}}.
        """), encoding="utf-8")
    bump_mtime(root / "schemas" / "memo.yaml")
    assert "memo" in schemas.record_kinds()


def test_composed_cues_must_fit_the_workers_declared_inputs(workers_dir):
    """The other side of the splice: a custom {kinds} worker that does
    not declare the date anchors cannot carry cues that use them — the
    format pass could never fill the slot. Refused at compose (save time
    and load time both), inside the VaultError contract."""
    root = workers_dir.parent
    (workers_dir / "rec.yaml").write_text(
        "inputs: [text]\n"
        "prompt: |\n  {kinds}\n  {text}\n"
        "schema:\n  type: object\n  properties:\n    record:\n"
        "      type: object\n      properties:\n        kind: {enum: [x]}\n",
        encoding="utf-8")
    (root / "schemas" / "memo.yaml").write_text(
        "record:\n  kind: memo\n  default: true\n"
        "  cues: |\n    - dated {today} memos\n", encoding="utf-8")
    with pytest.raises(VaultError, match=r"rec: record cues use placeholder\(s\) \['today'\]"):
        workers.get_worker("rec")


def test_record_kind_validation_names_the_file(workers_dir):
    root = workers_dir.parent
    (root / "schemas" / "bad.yaml").write_text(
        "record:\n  kind: bad\n  cues: '- x'\n  files_as: sideways\n", encoding="utf-8")
    with pytest.raises(VaultError, match="bad.yaml.*files_as"):
        schemas.record_kinds()


def test_two_defaults_is_refused(workers_dir):
    root = workers_dir.parent
    for name in ("one", "two"):
        (root / "schemas" / f"{name}.yaml").write_text(
            f"record:\n  kind: {name}\n  default: true\n  cues: '- x'\n",
            encoding="utf-8")
    with pytest.raises(VaultError, match="exactly one"):
        schemas.record_kinds()


def test_a_metadata_only_file_enforces_nothing(workers_dir):
    """capture.yaml's guarantee: a record-block-only file must never
    become a frontmatter schema — that would set schema_fix loose on
    every dated capture note."""
    root = workers_dir.parent
    (root / "schemas" / "capture.yaml").write_text(
        "description: only metadata\nrecord:\n  kind: capture\n"
        "  default: true\n  cues: '- x'\n", encoding="utf-8")
    assert schemas.load_schemas()["capture"] == {}
    assert schemas.validate_frontmatter("capture", {"anything": "goes"}) == []


def test_schema_edits_are_live_without_restart(workers_dir):
    root = workers_dir.parent
    p = root / "schemas" / "widget.yaml"
    p.write_text("type: object\nrequired: [id]\nproperties:\n  id: {type: string}\n",
                 encoding="utf-8")
    assert schemas.validate_frontmatter("widget", {}) != []
    p.write_text("type: object\nproperties:\n  id: {type: string}\n", encoding="utf-8")
    bump_mtime(p)
    assert schemas.validate_frontmatter("widget", {}) == []


# ---- schema-constrained decoding: enforce at the sampler when the
# endpoint can, fall back cleanly when it can't ----


def _fake_client(create):
    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)), base_url="http://fake:1/v1")


def _bad_request(msg="response_format not supported"):
    import httpx
    req = httpx.Request("POST", "http://fake:1/v1/chat/completions")
    return __import__("openai").BadRequestError(
        msg, response=httpx.Response(400, request=req), body=None)


def test_reply_schema_is_sent_as_json_schema(workers_dir):
    """The declared reply schema rides to the endpoint as
    response_format json_schema — grammar-enforcing servers make an
    invalid shape unproducible instead of merely caught."""
    (workers_dir / "testw.yaml").write_text(GOOD, encoding="utf-8")
    seen = []

    async def create(**kwargs):
        seen.append(kwargs["response_format"])
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps({"ok": "yes"})))])

    spec = workers.get_worker("testw")
    asyncio.run(workers.run_worker(spec, client=_fake_client(create), text="a", today="b"))
    assert seen[0]["type"] == "json_schema"
    assert seen[0]["json_schema"]["schema"] == spec.schema


def test_endpoint_without_json_schema_falls_back_and_remembers(workers_dir):
    """A 400 on the schema format costs one probe, never a judgment
    attempt and never a failed pass: same call retries as json_object,
    the endpoint is remembered, and the NEXT call skips the probe."""
    (workers_dir / "testw.yaml").write_text(GOOD, encoding="utf-8")
    workers._schema_format_ok.clear()
    seen = []

    async def create(**kwargs):
        seen.append(kwargs["response_format"]["type"])
        if kwargs["response_format"]["type"] == "json_schema":
            raise _bad_request()
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps({"ok": "yes"})))])

    spec = workers.get_worker("testw")
    client = _fake_client(create)
    stats: dict = {}
    out = asyncio.run(workers.run_worker(spec, client=client, stats=stats,
                                         text="a", today="b"))
    assert out == {"ok": "yes"}
    assert seen == ["json_schema", "json_object"]
    assert stats["attempts"] == 1  # the probe consumed no judgment attempt
    # memoized: the next call goes straight to the plain contract
    asyncio.run(workers.run_worker(spec, client=client, text="a", today="b"))
    assert seen[-1] == "json_object" and len(seen) == 3


def test_bad_request_on_plain_contract_is_a_clean_error(workers_dir):
    """A 400 that is NOT about response_format (context overflow, bad
    model name) surfaces as VaultError, not a stack trace."""
    (workers_dir / "testw.yaml").write_text(GOOD, encoding="utf-8")
    workers._schema_format_ok.clear()

    async def create(**kwargs):
        raise _bad_request("context length exceeded")

    spec = workers.get_worker("testw")
    with pytest.raises(VaultError, match="rejected the request"):
        asyncio.run(workers.run_worker(spec, client=_fake_client(create),
                                       text="a", today="b"))
