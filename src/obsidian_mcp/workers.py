"""Small dedicated LLM workers for narrow generative subtasks.

Experiment: instead of asking the orchestrator model to interpret messy
free text itself (or teaching it yet another tool convention), delegate
one tightly-scoped job — pulling structured tasks out of prose — to a
tiny model that only ever does that one thing. The worker never touches
the vault: it returns structured suggestions, and the caller decides
whether to act on them through the normal write-gated tools (add_task).
That split keeps the harness's write guarantees (git snapshots,
confirm_writes, format correctness) exactly where they already live.

Config via env, mirroring OBSIDIAN_VAULT_PATH:
  WORKER_BASE_URL - OpenAI-compatible endpoint (default: local Ollama)
  WORKER_MODEL    - model name (default: gemma4:e2b-it-q4_K_M)
"""

from __future__ import annotations

import json
import os

from openai import AsyncOpenAI

from .vault import VaultError

_client: AsyncOpenAI | None = None


def _worker_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        base_url = os.environ.get("WORKER_BASE_URL", "http://localhost:11434/v1")
        _client = AsyncOpenAI(base_url=base_url, api_key="ollama")
    return _client


def _worker_model() -> str:
    return os.environ.get("WORKER_MODEL", "gemma4:e2b-it-q4_K_M")


_EXTRACT_TASKS_PROMPT = """\
Extract every actionable task from the text below. A task is something a \
person needs to do — not a decision already made, not a general topic, not \
something that already happened.

Return ONLY a JSON object of this shape, nothing else:
{{"tasks": [{{"text": "...", "due": "YYYY-MM-DD or null", "priority": "highest|high|medium|low|lowest or null"}}]}}

Rules:
- due: a specific date only if stated or clearly implied by the text. Today is {today}.
- priority: only if stated or strongly implied (e.g. "urgent" -> highest). Otherwise null.
- text: the task itself, imperative mood, without repeating the date or priority word.
- No tasks found -> {{"tasks": []}}.

Text:
{text}
"""


async def extract_tasks(text: str, today: str) -> list[dict]:
    """Ask the worker model to pull structured tasks out of free text.

    Raises VaultError (not a bare exception) so the MCP tool layer can
    surface a clean message instead of a stack trace — same convention
    as the rest of the vault tools.
    """
    client = _worker_client()
    prompt = _EXTRACT_TASKS_PROMPT.format(today=today, text=text.strip())
    try:
        response = await client.chat.completions.create(
            model=_worker_model(),
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
    except Exception as e:  # connection refused, model not pulled, etc.
        raise VaultError(
            f"task-extraction worker ({_worker_model()}) unavailable: {e}"
        ) from None

    raw = response.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise VaultError(f"worker returned non-JSON output: {raw[:200]!r}") from None

    tasks = data.get("tasks", [])
    if not isinstance(tasks, list):
        raise VaultError(f"worker returned a malformed 'tasks' field: {tasks!r}")

    cleaned = []
    for t in tasks:
        if not isinstance(t, dict) or not str(t.get("text", "")).strip():
            continue
        cleaned.append(
            {
                "text": str(t["text"]).strip(),
                "due": t.get("due") or None,
                "priority": t.get("priority") or None,
            }
        )
    return cleaned
