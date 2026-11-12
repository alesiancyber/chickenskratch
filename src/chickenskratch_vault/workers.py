"""Small dedicated LLM workers — the one bounded-LLM-call
mechanism in this package.

A worker is a WorkerSpec: prompt template, jsonschema for the reply,
timeout, temperature. Specs live as config files — one YAML per worker
under config/workers/ (vault.yaml `workers_dir:`) — loaded through
get_worker() with an mtime-keyed cache, so a prompt edit is live the
next call, no restart. build_worker() is the single validator: the
loader, the console's save path, and the test-run endpoint all build
specs through it. run_worker() makes one call with one retry, parses
and validates the reply against the spec's schema (jsonval — a reply
that drops required fields fails loud instead of being silently
cleaned), and surfaces every failure as VaultError. Workers are
read-only: they return structured suggestions; the caller acts on them
through the normal write-gated tools, so the harness's write guarantees
stay exactly where they already live.

Prompts are written for a CAPABLE model — the loose reading is the
out-of-the-box behavior. Small-model hedging is the adapt layer: each
spec may carry a `hedges` block of extra conservative rules, applied
only when WORKER_ADAPT says the configured model needs them. Hedging
by capability tier beats trying to decide, rule by rule, which
strictness a given model has outgrown.

Config via env, mirroring CHICKENSKRATCH_VAULT_PATH:
  WORKER_BASE_URL  - OpenAI-compatible endpoint (default: the mlx-worker
                     backend from models.yaml, port 8081)
  WORKER_MODEL     - model name (default: Qwen3.5-4B MLX, the current
                     worker-eval winner — see evals/reports/workers/;
                     needs thinking disabled, see the mlx-worker
                     backend command)
  WORKER_ADAPT     - "small" appends each spec's hedge rules; "none"
                     runs the loose base prompt. Default "small": it
                     pairs with the built-in 4B default above, and an
                     unhedged small model is worse than a hedged big
                     one. Filled from the worker profile's
                     `worker_adapt:` by bootstrap, like the rest.
  WORKER_TIMEOUT_S - per-call timeout override (default: the spec's own;
                     the OpenAI client default of 600s turns a slow or
                     thinking-mode model into an apparent hang)
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import yaml
from openai import AsyncOpenAI, BadRequestError

from . import jsonval, schemas
from . import tasks as tk
from .vault import VaultError
from .vaultconfig import get_config

# The mlx-worker backend from models.yaml. Every reader of the worker's
# endpoint and model goes through the two accessors below — health lines
# and dashboards were each re-deriving these defaults from the env.
DEFAULT_BASE_URL = "http://127.0.0.1:8081/v1"
DEFAULT_MODEL = "mlx-community/Qwen3.5-4B-MLX-4bit"

_client: AsyncOpenAI | None = None


def worker_base_url() -> str:
    return os.environ.get("WORKER_BASE_URL", DEFAULT_BASE_URL)


def worker_model() -> str:
    return os.environ.get("WORKER_MODEL", DEFAULT_MODEL)


def worker_adapt() -> str:
    """The capability tier the prompts adapt to. "small" (the default —
    it pairs with the built-in 4B DEFAULT_MODEL) appends each spec's
    hedge rules; anything else runs the loose base prompt."""
    return os.environ.get("WORKER_ADAPT", "small")


def new_client() -> AsyncOpenAI:
    """A worker-endpoint client from the current env. The console's
    test-run endpoint needs its own (the singleton below is bound to the
    engine thread's event loop) — same construction, one spelling.
    Cloud worker endpoints need real auth; local servers ignore the key."""
    return AsyncOpenAI(base_url=worker_base_url(),
                       api_key=os.environ.get("WORKER_API_KEY") or "ollama")


def _worker_client() -> AsyncOpenAI:
    global _client
    c = _client
    if c is None:
        c = _client = new_client()
    return c


# Closes in flight, held so the loop can't collect them mid-close.
_closing: set = set()


def reset_client() -> None:
    """Drop the worker client so the next call rebuilds it against the
    current endpoint and key — and close the old one's connection pool.

    Assigning None alone leaks an httpx pool (and its open sockets) every
    time a vault's Spec.md `worker:` re-points the endpoint, which is
    once per pass for a vault that keeps switching. The close is
    scheduled on the running loop because this is called from
    synchronous config code; with no loop there is nothing to schedule
    on and the pool dies with the process, which is the same outcome by
    a blunter route."""
    global _client
    old, _client = _client, None
    if old is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(old.close())
    _closing.add(task)
    task.add_done_callback(_closing.discard)


def _splice(template: str, slot: str, text: str) -> str:
    """Replace a machine-supplied {slot} BEFORE the format pass, so the
    spliced text may use the same placeholders as the base prompt. Two
    replaces, because the slot must be gone either way: the first eats
    the slot's own line (an empty splice doesn't leave a blank one), the
    second catches a template that wrote the slot inline. A slot
    surviving into .format() is a KeyError raised inside run_worker —
    outside its VaultError contract, so it would crash every pass
    instead of failing one processor. One implementation for hedges and
    kinds both: the two eat-the-line rules can't drift apart."""
    marker = "{" + slot + "}"
    template = template.replace(marker + "\n", f"{text}\n" if text else "")
    return template.replace(marker, text)


def render_prompt(spec: WorkerSpec, **inputs) -> str:
    """The exact prompt a call sends: hedges spliced per WORKER_ADAPT,
    then the one format pass. Shared by run_worker and the console's
    test-run preview, so the preview can never drift from the call."""
    hedges = spec.hedges.rstrip("\n") if spec.hedges and worker_adapt() == "small" else ""
    return _splice(spec.prompt, "hedges", hedges).format(**inputs)


# (endpoint, worker) -> whether the endpoint accepted json_schema
# enforcement. Learned by probe: a 400 on the schema format falls back
# to plain json_object and is remembered, so an endpoint without
# grammar support (or one exotic schema) costs one extra request once,
# never a failed pass. Keyed per worker so one unconvertible schema
# doesn't strip enforcement from the other six.
_schema_format_ok: dict[tuple[str, str], bool] = {}


def _response_format(spec: WorkerSpec, endpoint: str) -> dict:
    """The strongest reply-shape enforcement this endpoint accepts for
    this worker. With json_schema (llama.cpp grammar sampling, OpenAI
    structured outputs) a misspelled enum, malformed date, or missing
    required field becomes impossible AT THE SAMPLER; json_object is
    the fallback contract. jsonval still validates the reply either
    way — enforcement decides whether an invalid shape can be
    produced, not whether it would be caught."""
    if _schema_format_ok.get((endpoint, spec.name), True):
        return {"type": "json_schema",
                "json_schema": {"name": spec.name, "strict": True,
                                "schema": spec.schema}}
    return {"type": "json_object"}


def _env_number(cast, name: str, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        print(f"{name}={raw!r} is not a number; using {default}", file=sys.stderr)
        return default


@dataclass(frozen=True)
class WorkerSpec:
    name: str            # "extract_tasks"
    prompt: str          # template; placeholders filled by run_worker(**inputs)
    schema: dict         # jsonschema the parsed reply must satisfy
    timeout_s: float = 60.0
    temperature: float = 0.1
    max_tokens: int = 2000   # bound runaway generation
    # The adapt layer: extra conservative rules spliced into the prompt's
    # {hedges} slot when WORKER_ADAPT is "small". The base prompt is the
    # capable-model reading; hedges narrow it for models that wander.
    # The reply SCHEMA is never tiered — it is the API, not a hedge.
    hedges: str = ""
    # The prompt placeholders a caller must fill — the declared contract
    # between the config file and the code that invokes it.
    inputs: tuple[str, ...] = ()
    description: str = ""   # console prose: what this worker is for

    def __post_init__(self) -> None:
        # Fail at load, not silently at draft time: hedges with nowhere
        # to splice would just vanish, and the first sign would be a small
        # model quietly running the loose prompt.
        if self.hedges and "{hedges}" not in self.prompt:
            raise ValueError(f"{self.name}: hedges set but prompt has no {{hedges}} slot")


async def run_worker(spec: WorkerSpec, client: AsyncOpenAI | None = None,
                     stats: dict | None = None, **inputs) -> dict:
    """One bounded call: format the prompt, require JSON out, validate it
    against the spec's schema. One retry on junk output; every failure is
    a VaultError so the MCP tool layer surfaces a clean message.

    `client` overrides the module singleton — the console's test-run
    endpoint passes a fresh one because the singleton is bound to the
    engine thread's event loop.

    `stats`, when given, is filled with call telemetry ({"attempts": n}).
    The eval runner reads it because the silent retry otherwise HIDES
    marginal schema discipline: a model that needs the retry every call
    scores identical to one that never does, while being twice as slow
    and one bad sample from failing. Kept an out-param so the reply
    shape — the API every caller parses — never changes."""
    client = client or _worker_client()
    if stats is not None:
        stats["attempts"] = 0
    # The declared contract, checked before .format() so a caller that
    # drifted from the config file's inputs fails as a clean VaultError,
    # not a KeyError outside run_worker's error contract.
    missing = sorted(set(spec.inputs) - set(inputs))
    if missing:
        raise VaultError(f"{spec.name} worker: missing input(s) {missing}")
    prompt = render_prompt(spec, **inputs)
    # Env parses degrade to the spec default on garbage ("60s", ""): a
    # ValueError here is outside run_worker's VaultError contract, and an
    # .env typo must never become a crash of every engine pass.
    timeout = _env_number(float, "WORKER_TIMEOUT_S", spec.timeout_s)
    # Reasoning models spend max_tokens on hidden thought BEFORE the
    # JSON, so a ceiling sized for local workers truncates mid-object.
    # The env override lifts it without unbounding local workers.
    max_tokens = _env_number(int, "WORKER_MAX_TOKENS", spec.max_tokens)
    last_error = ""
    # getattr: test doubles and thin fakes don't carry base_url; ""
    # simply gives them their own memo slot.
    endpoint = str(getattr(client, "base_url", "") or "")
    attempt = 0
    while attempt < 2:
        fmt = _response_format(spec, endpoint)
        try:
            response = await client.chat.completions.create(
                model=worker_model(),
                messages=[{"role": "user", "content": prompt}],
                response_format=fmt,
                temperature=spec.temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        except BadRequestError as e:
            if fmt["type"] == "json_schema":
                # The endpoint (or this schema) rejects grammar
                # enforcement: remember, and retry the SAME attempt on
                # the plain contract — the probe must never consume one
                # of the two judgment attempts.
                _schema_format_ok[(endpoint, spec.name)] = False
                continue
            raise VaultError(
                f"{spec.name} worker ({worker_model()}) rejected the request: {e}") from None
        except Exception as e:  # connection refused, model not pulled, etc.
            raise VaultError(f"{spec.name} worker ({worker_model()}) unavailable: {e}") from None
        attempt += 1
        if stats is not None:
            stats["attempts"] = attempt

        # A reasoning model that spends the whole token budget thinking
        # returns choices=[] — that must be the clean error below, not an
        # IndexError.
        raw = response.choices[0].message.content if response.choices else ""
        if not raw or not raw.strip():
            # A thinking-mode model puts everything in `reasoning` and leaves
            # content empty — a config problem no retry fixes. Fail loud.
            raise VaultError(
                f"worker ({worker_model()}) returned empty content — thinking-mode "
                "model? Disable thinking (e.g. mlx_lm.server --chat-template-args "
                "'{\"enable_thinking\": false}') or use a non-thinking variant."
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            last_error = f"non-JSON output: {raw[:200]!r}"
            continue
        issues = jsonval.validate(data, spec.schema)
        if issues:
            last_error = f"schema-invalid reply: {'; '.join(issues[:3])}"
            continue
        return data
    raise VaultError(f"{spec.name} worker ({worker_model()}) returned {last_error}")


# The workers the engine's processors and query paths invoke by name.
# Their files ship with the repo and may be edited (prompts, hedges,
# bounds) but never deleted; their `inputs` are the contract the calling
# code compiles against, so the console shows those read-only.
RESERVED_WORKERS = frozenset({
    "answer", "schema_fix", "extract_tasks", "extract_record",
    "plan_today", "overlap", "grouping",
})

# Slots build_worker treats as machine-supplied, never caller inputs:
# {hedges} splices in run_worker; {kinds}/{kind_choices} compose from
# the schema files' record: blocks in get_worker, so extract_record's
# kind list is generated, never hand-written.
_GENERATED_SLOTS = frozenset({"hedges", "kinds", "kind_choices"})

_WORKER_KEYS = frozenset({
    "description", "inputs", "timeout_s", "temperature", "max_tokens",
    "prompt", "hedges", "schema",
})


def workers_dir() -> Path:
    return Path(get_config().workers_dir)


def worker_names() -> list[str]:
    d = workers_dir()
    return sorted(p.stem for p in d.glob("*.yaml")) if d.is_dir() else []


def build_worker(name: str, text: str) -> WorkerSpec:
    """Parse + validate ONE worker document. The single validator: the
    file loader, the console's save path, and the test-run endpoint all
    come through here, so a file that loads is a file that runs."""
    if not schemas.NAME_RE.match(name):
        raise VaultError(f"worker name {name!r} must match {schemas.NAME_RE.pattern}")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise VaultError(f"{name}.yaml: not valid YAML: {e}") from None
    if not isinstance(doc, dict):
        raise VaultError(f"{name}.yaml: expected a mapping at top level")
    unknown = sorted(set(doc) - _WORKER_KEYS)
    if unknown:
        raise VaultError(f"{name}.yaml: unknown key(s) {unknown}")
    prompt = doc.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise VaultError(f"{name}.yaml: needs a prompt")
    schema = doc.get("schema")
    if not isinstance(schema, dict) or not schema:
        raise VaultError(f"{name}.yaml: needs a reply schema (a mapping)")
    issues = jsonval.check_schema(schema)
    if issues:
        raise VaultError(f"{name}.yaml: reply schema is not valid jsonschema: {issues[0]}")
    raw_inputs = doc.get("inputs") or []
    if not isinstance(raw_inputs, list) or not all(isinstance(i, str) for i in raw_inputs):
        raise VaultError(f"{name}.yaml: inputs must be a list of placeholder names")
    inputs = tuple(raw_inputs)
    hedges = doc.get("hedges") or ""
    if not isinstance(hedges, str):
        raise VaultError(f"{name}.yaml: hedges must be text")
    # The placeholder/inputs contract: every {slot} the prompt names must
    # be a declared input (or machine-supplied), and every declared input
    # must appear — an unused input is a typo about to become a KeyError.
    declared = set(inputs)
    used = schemas.template_placeholders(prompt) - _GENERATED_SLOTS
    if used != declared:
        extra, unused = sorted(used - declared), sorted(declared - used)
        parts = []
        if extra:
            parts.append(f"prompt uses undeclared placeholder(s) {extra}")
        if unused:
            parts.append(f"declared input(s) {unused} never appear in the prompt")
        raise VaultError(f"{name}.yaml: {'; '.join(parts)}")
    stray = sorted(schemas.template_placeholders(hedges) - _GENERATED_SLOTS - declared)
    if stray:
        raise VaultError(f"{name}.yaml: hedges use undeclared placeholder(s) {stray}")

    def _num(key, cast, default, lo, hi):
        v = doc.get(key, default)
        try:
            v = cast(v)
        except (TypeError, ValueError):
            raise VaultError(f"{name}.yaml: {key} must be a number") from None
        if not lo <= v <= hi:
            raise VaultError(f"{name}.yaml: {key} must be between {lo} and {hi}")
        return v

    try:
        return WorkerSpec(
            name=name,
            prompt=prompt,
            schema=schema,
            timeout_s=_num("timeout_s", float, 60.0, 1, 3600),
            temperature=_num("temperature", float, 0.1, 0, 2),
            max_tokens=int(_num("max_tokens", int, 2000, 1, 1_000_000)),
            hedges=hedges,
            inputs=inputs,
            description=str(doc.get("description") or "").strip(),
        )
    except ValueError as e:  # __post_init__: hedges without a {hedges} slot
        raise VaultError(f"{name}.yaml: {e}") from None


# name -> (file path, mtime, built spec). Stat on every access, rebuilt
# on any mtime change — the get_config pattern, never a forever-cache.
# A failing parse raises without assigning, so a bad save can't poison
# the cache with a partial spec.
_worker_cache: dict[str, tuple[str, float, WorkerSpec]] = {}


def get_worker(name: str) -> WorkerSpec:
    """The named worker, fresh from its file (mtime-checked per call)."""
    path = workers_dir() / f"{name}.yaml"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        raise VaultError(f"no worker {name!r} ({path} not found)") from None
    hit = _worker_cache.get(name)
    if hit is None or hit[0] != str(path) or hit[1] != mtime:
        spec = build_worker(name, path.read_text(encoding="utf-8"))
        _worker_cache[name] = (str(path), mtime, spec)
    return compose_kinds(_worker_cache[name][2])


def compose_kinds(spec: WorkerSpec) -> WorkerSpec:
    """Splice the record kinds the schema files declare into a spec whose
    prompt carries {kinds}/{kind_choices}, and pin the reply schema's
    record.kind enum to those names. Composed on every access — cheap
    string work — so a schema edit is live the moment load_schemas'
    mtime key moves, with no third cache to invalidate."""
    if "{kinds}" not in spec.prompt and "{kind_choices}" not in spec.prompt:
        return spec
    kinds = schemas.record_kinds()
    if not kinds:
        raise VaultError(
            f"{spec.name}: prompt expects record kinds, but no schema file "
            "declares a record: block")
    ordered = ([k for k in sorted(kinds.values(), key=lambda k: k.name) if not k.default]
               + [k for k in kinds.values() if k.default])
    bullets = [k.cues.rstrip("\n") for k in ordered]
    bullets += [k.guidance.rstrip("\n") for k in ordered if k.guidance]
    kinds_text = "\n".join(bullets)
    # parse_record_block already limits cue placeholders to the date
    # anchors; this checks the OTHER side of the splice — a worker that
    # composes kinds but doesn't declare those anchors as inputs would
    # carry a slot the format pass can't fill.
    stray = sorted(schemas.template_placeholders(kinds_text) - set(spec.inputs))
    if stray:
        raise VaultError(
            f"{spec.name}: record cues use placeholder(s) {stray} that are not "
            "among this worker's declared inputs")
    choices = " or ".join(k.name for k in ordered)
    prompt = _splice(_splice(spec.prompt, "kinds", kinds_text),
                     "kind_choices", choices)
    schema = copy.deepcopy(spec.schema)
    try:
        schema["properties"]["record"]["properties"]["kind"]["enum"] = \
            [k.name for k in ordered]
    except (KeyError, TypeError):
        raise VaultError(
            f"{spec.name}: reply schema has no record.kind property to "
            "carry the generated kind enum") from None
    return replace(spec, prompt=prompt, schema=schema)


def _real_date_or_none(value) -> str | None:
    """The reply schema's pattern only pins digit shape — an impossible
    date like 2026-13-01 passes it. Downstream every date is assumed
    fromisoformat-able (format_task raises, Today.md parses), so an
    impossible one is dropped here rather than poisoning a draft."""
    if not value:
        return None
    try:
        dt.date.fromisoformat(str(value))
    except ValueError:
        return None
    return str(value)


def _shape_tasks(raw_tasks: list[dict]) -> list[dict]:
    """The worker's task rows, sanitized the one way every extractor
    shares: real dates or None, dropped impossible dates surfaced as
    `dropped_due` (drafters turn that into a forced checkbox), and
    `depth` made well-formed (first task 0, no level skipped — the
    only nesting a list of indented lines can express; a model that
    omits the field gets a flat list, exactly as before)."""
    out = []
    depths = tk.normalize_depths([_int_or_zero(t.get("depth")) for t in raw_tasks])
    for t, depth in zip(raw_tasks, depths, strict=True):
        due = _real_date_or_none(t.get("due"))
        scheduled = _real_date_or_none(t.get("scheduled"))
        task = {
            "text": t["text"].strip(),
            "due": due,
            "scheduled": scheduled,
            "priority": t.get("priority") or None,
            "owed_by": (t.get("owed_by") or "").strip() or None,
            "depth": depth,
        }
        if t.get("due") and due is None:
            # Surfaced, not swallowed: drafters turn this into a forced
            # checkbox — losing a date silently in an auto-applied task
            # would hide exactly the kind of drift the verifiers exist for.
            task["dropped_due"] = str(t["due"])
        if t.get("scheduled") and scheduled is None:
            task["dropped_due"] = str(t["scheduled"])  # same forced-review lever
        out.append(task)
    return out


def _int_or_zero(value) -> int:
    """A depth the schema let through as a number, or 0 for anything
    else (null, a string, a bool)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _week_anchors(today: str) -> dict:
    """The prompt's date context: today plus the ⏳ anchor Sundays
    ("this week" / "next week" plan language) — tasks.week_end, the one
    anchor every surface shares."""
    this_sunday = tk.week_end(dt.date.fromisoformat(today))
    return {"today": today, "this_sunday": this_sunday.isoformat(),
            "next_sunday": (this_sunday + dt.timedelta(days=7)).isoformat()}


async def extract_tasks(text: str, today: str, stats: dict | None = None) -> list[dict]:
    """Pull structured tasks out of free text via the extract_tasks worker.
    Week-granular plan language ("this week", "next week") lands in
    `scheduled` as that week's Sunday — the ⏳ anchor convention."""
    data = await run_worker(get_worker("extract_tasks"), stats=stats,
                            **_week_anchors(today), text=text.strip())
    return _shape_tasks(data["tasks"])


async def extract_record(text: str, today: str, stats: dict | None = None) -> dict:
    """Decompose a dump into {record, tasks} via the extract_record
    worker — the capture lane's one LLM call. The record's summary is a
    free rewrite (reviewed, never verified); the tasks are sanitized
    exactly like extract_tasks' so the drafter holds them to the same
    fidelity standard."""
    data = await run_worker(get_worker("extract_record"), stats=stats,
                            **_week_anchors(today), text=text.strip())
    rec = data["record"]
    return {
        "record": {
            "kind": rec.get("kind") or schemas.default_record_kind(),
            "title": (rec.get("title") or "").strip(),
            "attendees": [a.strip() for a in rec.get("attendees") or [] if a.strip()],
            "summary": (rec.get("summary") or "").strip(),
        },
        "tasks": _shape_tasks(data["tasks"]),
    }
