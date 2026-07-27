"""Small dedicated LLM workers — the one bounded-LLM-call
mechanism in this package.

A worker is a WorkerSpec: prompt template, jsonschema for the reply,
timeout, temperature. run_worker() makes one call with one retry, parses
and validates the reply against the spec's schema (jsonval — a reply
that drops required fields fails loud instead of being silently
cleaned), and surfaces every failure as VaultError. Workers are
read-only: they return structured suggestions; the caller acts on them
through the normal write-gated tools, so the harness's write guarantees
stay exactly where they already live.

Config via env, mirroring TEZCAT_VAULT_PATH:
  WORKER_BASE_URL  - OpenAI-compatible endpoint (default: the mlx-worker
                     backend from models.yaml, port 8081)
  WORKER_MODEL     - model name (default: Qwen3.5-4B MLX, the current
                     worker-eval winner — see evals/reports/workers/;
                     needs thinking disabled, see the mlx-worker
                     backend command)
  WORKER_TIMEOUT_S - per-call timeout override (default: the spec's own;
                     the OpenAI client default of 600s turns a slow or
                     thinking-mode model into an apparent hang)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from dataclasses import dataclass

from openai import AsyncOpenAI

from . import jsonval
from .vault import VaultError

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


def _worker_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        # Cloud worker endpoints need real auth; local servers ignore the key.
        _client = AsyncOpenAI(base_url=worker_base_url(),
                              api_key=os.environ.get("WORKER_API_KEY") or "ollama")
    return _client


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


async def run_worker(spec: WorkerSpec, **inputs) -> dict:
    """One bounded call: format the prompt, require JSON out, validate it
    against the spec's schema. One retry on junk output; every failure is
    a VaultError so the MCP tool layer surfaces a clean message."""
    client = _worker_client()
    prompt = spec.prompt.format(**inputs)
    # Env parses degrade to the spec default on garbage ("60s", ""): a
    # ValueError here is outside run_worker's VaultError contract, and an
    # .env typo must never become a crash of every engine pass.
    timeout = _env_number(float, "WORKER_TIMEOUT_S", spec.timeout_s)
    # Reasoning models spend max_tokens on hidden thought BEFORE the
    # JSON, so a ceiling sized for local workers truncates mid-object.
    # The env override lifts it without unbounding local workers.
    max_tokens = _env_number(int, "WORKER_MAX_TOKENS", spec.max_tokens)
    last_error = ""
    for _attempt in range(2):
        try:
            response = await client.chat.completions.create(
                model=worker_model(),
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=spec.temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        except Exception as e:  # connection refused, model not pulled, etc.
            raise VaultError(f"{spec.name} worker ({worker_model()}) unavailable: {e}") from None

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


_EXTRACT_TASKS_PROMPT = """\
Extract every actionable task from the text below. A task is something a \
person needs to do — not a decision already made, not a general topic, not \
something that already happened.

Return ONLY a JSON object of this shape, nothing else:
{{"tasks": [{{"text": "...", "due": "YYYY-MM-DD or null", "scheduled": "YYYY-MM-DD or null", "priority": "highest|high|medium|low|lowest or null", "owed_by": "name or null"}}]}}

Rules:
- due is a HARD deadline: a specific date only when the text states or
  clearly implies a real commitment ("by Friday", "due the 30th").
  Today is {today}.
- scheduled is a PLAN, not a deadline — when the text says when the
  person intends to work on it: "this week" -> {this_sunday},
  "next week" -> {next_sunday}, "today"/"tonight" -> {today}. Never
  invent one, and never set both due and scheduled from the same phrase:
  deadline words go to due, plan words go to scheduled.
- priority: only if stated or strongly implied (e.g. "urgent" -> highest). Otherwise null.
- owed_by: when someone ELSE committed to doing or providing something \
("Nadia will send X", "Marcus said he'll get me Y"), extract it too — it is \
a follow-up, with owed_by set to that person's name and text describing what \
they owe. The reader's own actions (including "we" or "someone on our side") \
have owed_by null.
- text: the task itself, imperative mood, without repeating the date or priority word.
- A decision already made, or a plan someone reviewed or approved, is NOT a \
task unless the text asks someone to carry it out. But a suggestion for future \
work ("we should...", "someday...") IS a task, with due and priority null \
unless stated.
- No tasks found -> {{"tasks": []}}.

Text:
{text}
"""

_SCHEMA_FIX_PROMPT = """\
You maintain a markdown vault. The note below fails its entity schema.
Draft the minimal fix as tool calls. Allowed tools and their args:
- update_frontmatter: {{"path": ..., "key": ..., "value": ...}}  (value is a YAML string)
- replace_section:    {{"path": ..., "heading": ..., "content": ...}}
- append_to_note:     {{"path": ..., "content": ..., "section": ...}}

Rules:
- Use the note path exactly as given: {note}
- If the schema requires an id in frontmatter, set it to exactly: {new_id}
- Task lines missing an id get a block ref appended (last token on the
  line, e.g. ` ^t-1a2b3c`). Use these ids in order, one per line: {extra_ids}
- Fix ONLY the listed issues. Keep every task's text, dates, and priorities
  byte-identical; never invent, drop, or reword content.

Return ONLY JSON of this shape, nothing else:
{{"title": "short human title", "summary": "one sentence", "apply": [{{"tool": "...", "args": {{...}}}}]}}

Entity type: {entity}
Issues:
{issues}

Note content:
{content}
"""

_PLAN_TODAY_PROMPT = """\
You plan one day of work. Today is {today}.

From the undated backlog below, choose which tasks the user should work
on TODAY. These have no deadlines — this is pure judgment.

Return ONLY a JSON object of this shape, nothing else:
{{"picks": [{{"id": "t-...", "reason": "short clause, under 12 words"}}]}}

Rules:
- Pick 0 to {max_picks}. Picking NONE is a correct answer — return
  {{"picks": []}} when nothing stands out. Never pad the list.
- The user already has {committed} task(s) committed today. If that is
  3 or more, pick at most 1.
- Prefer: tasks that unblock other people, quick wins that clear
  mental load, items that have sat longest, and anything whose wording
  implies time pressure or an external party waiting.
- Use ids exactly as given; never invent one.

Backlog (id: text — age, priority):
{tasks}
"""

_OVERLAP_PROMPT = """\
You judge whether two task lines describe the same piece of work.

For EVERY numbered pair below, return one verdict:
- "duplicate": both lines describe the same work — finishing one finishes
  the other. Also say which id to KEEP: prefer the line with dates or
  priority, then the more precise wording.
- "related": same area or dependent work, but NOT interchangeable —
  completing one does not complete the other.
- "distinct": different work, even when the wording is similar.

Return ONLY a JSON object of this shape, nothing else:
{{"pairs": [{{"a": "t-...", "b": "t-...", "verdict": "duplicate", "keep": "t-... or null"}}]}}

Rules:
- Answer every pair exactly once, ids exactly as given, never invented.
- Tasks for different clients, projects, or systems are NEVER duplicates,
  even with near-identical wording.
- "keep" is set only for duplicates; otherwise null.

Pairs:
{pairs}
"""

_GROUPING_PROMPT = """\
Organize the flat task list below into a few thematic groups.

Return ONLY a JSON object of this shape, nothing else:
{{"groups": [{{"name": "Short group heading", "member_ids": ["t-..."]}}]}}

Rules:
- Every task id appears in exactly one group. Never invent, drop, or repeat ids.
- Group by what the work is actually about. Tasks that belong to different
  clients, projects, or systems must NEVER share a group, even when their
  wording is similar.
- Use 2 to 6 groups. A task that fits nowhere goes in a group named "Ungrouped".
- Group names are short noun phrases, not sentences.

Tasks (id: text):
{tasks}
"""

WORKERS: dict[str, WorkerSpec] = {
    "schema_fix": WorkerSpec(
        name="schema_fix",
        prompt=_SCHEMA_FIX_PROMPT,
        temperature=0.1,
        max_tokens=4000,   # plans can carry a rewritten section
        schema={
            "type": "object",
            "required": ["title", "summary", "apply"],
            "properties": {
                "title": {"type": "string", "minLength": 1},
                "summary": {"type": "string"},
                "apply": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["tool", "args"],
                        "properties": {
                            "tool": {
                                "enum": ["update_frontmatter", "replace_section", "append_to_note"]
                            },
                            "args": {"type": "object"},
                        },
                    },
                },
            },
        },
    ),
    "extract_tasks": WorkerSpec(
        name="extract_tasks",
        prompt=_EXTRACT_TASKS_PROMPT,
        schema={
            "type": "object",
            "required": ["tasks"],
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["text"],
                        "properties": {
                            "text": {"type": "string", "minLength": 1},
                            "due": {
                                "type": ["string", "null"],
                                "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
                            },
                            "scheduled": {
                                "type": ["string", "null"],
                                "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
                            },
                            "priority": {
                                "enum": ["highest", "high", "medium", "low", "lowest", None]
                            },
                            "owed_by": {"type": ["string", "null"]},
                        },
                    },
                }
            },
        },
    ),
    "plan_today": WorkerSpec(
        name="plan_today",
        prompt=_PLAN_TODAY_PROMPT,
        temperature=0.1,
        schema={
            "type": "object",
            "required": ["picks"],
            "properties": {
                "picks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["id"],
                        "properties": {
                            "id": {"type": "string", "minLength": 1},
                            "reason": {"type": "string"},
                        },
                    },
                }
            },
        },
    ),
    "overlap": WorkerSpec(
        name="overlap",
        prompt=_OVERLAP_PROMPT,
        temperature=0.1,
        schema={
            "type": "object",
            "required": ["pairs"],
            "properties": {
                "pairs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["a", "b", "verdict"],
                        "properties": {
                            "a": {"type": "string", "minLength": 1},
                            "b": {"type": "string", "minLength": 1},
                            "verdict": {"enum": ["duplicate", "related", "distinct"]},
                            "keep": {"type": ["string", "null"]},
                        },
                    },
                }
            },
        },
    ),
    "grouping": WorkerSpec(
        name="grouping",
        prompt=_GROUPING_PROMPT,
        temperature=0.1,
        schema={
            "type": "object",
            "required": ["groups"],
            "properties": {
                "groups": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["name", "member_ids"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "member_ids": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string"},
                            },
                        },
                    },
                }
            },
        },
    ),
}


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


async def extract_tasks(text: str, today: str) -> list[dict]:
    """Pull structured tasks out of free text via the extract_tasks worker.
    Week-granular plan language ("this week", "next week") lands in
    `scheduled` as that week's Sunday — the ⏳ anchor convention."""
    day = dt.date.fromisoformat(today)
    this_sunday = day + dt.timedelta(days=6 - day.weekday())
    data = await run_worker(WORKERS["extract_tasks"], today=today,
                            this_sunday=this_sunday.isoformat(),
                            next_sunday=(this_sunday + dt.timedelta(days=7)).isoformat(),
                            text=text.strip())
    out = []
    for t in data["tasks"]:
        due = _real_date_or_none(t.get("due"))
        scheduled = _real_date_or_none(t.get("scheduled"))
        task = {
            "text": t["text"].strip(),
            "due": due,
            "scheduled": scheduled,
            "priority": t.get("priority") or None,
            "owed_by": (t.get("owed_by") or "").strip() or None,
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
