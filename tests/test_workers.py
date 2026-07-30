"""Fast unit tests for the task-extraction worker's parsing/robustness.

No network, no model: a scripted fake client stands in for the worker
LLM, same pattern as test_agent_guarantees.py's FakeOpenAI. What's under
test is workers.extract_tasks' handling of the worker's output, not the
worker model's judgment (that's exercised live, not in CI).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from chickenskratch_vault import workers
from chickenskratch_vault.vault import VaultError


def fake_client(content: str | None = None, raises: Exception | None = None):
    async def create(**kwargs):
        if raises:
            raise raises
        msg = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_extracts_well_formed_tasks(monkeypatch):
    payload = {
        "tasks": [
            {"text": "Migrate DNS", "due": "2026-07-24", "priority": "high"},
            {"text": "Revisit the pricing page", "due": None, "priority": None},
            {"text": "Send the vendor contract", "due": None, "priority": None, "owed_by": "Nadia"},
        ]
    }
    monkeypatch.setattr(workers, "_worker_client", lambda: fake_client(json.dumps(payload)))
    tasks = asyncio.run(workers.extract_tasks("Rio needs to migrate DNS by Friday...", "2026-07-20"))
    # owed_by and scheduled are normalized onto every item (absent -> None)
    assert tasks == [
        {"text": "Migrate DNS", "due": "2026-07-24", "scheduled": None,
         "priority": "high", "owed_by": None},
        {"text": "Revisit the pricing page", "due": None, "scheduled": None,
         "priority": None, "owed_by": None},
        {"text": "Send the vendor contract", "due": None, "scheduled": None,
         "priority": None, "owed_by": "Nadia"},
    ]


def test_no_tasks_found(monkeypatch):
    monkeypatch.setattr(workers, "_worker_client", lambda: fake_client('{"tasks": []}'))
    tasks = asyncio.run(workers.extract_tasks("Just a status update, nothing to do.", "2026-07-20"))
    assert tasks == []


def test_entries_missing_text_fail_loud(monkeypatch):
    # Silently dropping a malformed entry is a silently-missing task —
    # the exact failure class the schema contract exists to prevent.
    payload = {"tasks": [{"due": "2026-07-24"}, {"text": "Real task", "due": None, "priority": None}]}
    monkeypatch.setattr(workers, "_worker_client", lambda: fake_client(json.dumps(payload)))
    with pytest.raises(VaultError, match="schema-invalid"):
        asyncio.run(workers.extract_tasks("...", "2026-07-20"))


def test_empty_content_raises_clean_error(monkeypatch):
    # Thinking-mode models fill `reasoning` and leave content None — that
    # must be a loud error, never a silent "no tasks found".
    monkeypatch.setattr(workers, "_worker_client", lambda: fake_client(content=None))
    with pytest.raises(VaultError, match="empty content"):
        asyncio.run(workers.extract_tasks("...", "2026-07-20"))


def test_non_json_output_raises_clean_error(monkeypatch):
    monkeypatch.setattr(
        workers, "_worker_client", lambda: fake_client("sure, here are the tasks:\n- do a thing")
    )
    with pytest.raises(VaultError, match="non-JSON"):
        asyncio.run(workers.extract_tasks("...", "2026-07-20"))


def test_malformed_tasks_field_raises_clean_error(monkeypatch):
    monkeypatch.setattr(workers, "_worker_client", lambda: fake_client('{"tasks": "do a thing"}'))
    with pytest.raises(VaultError, match="schema-invalid"):
        asyncio.run(workers.extract_tasks("...", "2026-07-20"))


def test_worker_unavailable_raises_clean_error(monkeypatch):
    monkeypatch.setattr(
        workers, "_worker_client", lambda: fake_client(raises=ConnectionError("refused"))
    )
    with pytest.raises(VaultError, match="unavailable"):
        asyncio.run(workers.extract_tasks("...", "2026-07-20"))


def test_impossible_due_dates_are_dropped(monkeypatch):
    """The reply schema's pattern only pins shape — 2026-13-01 passes it.
    Downstream assumes every date is real (format_task raises mid-draft,
    Today.md parses), so extract_tasks nulls it instead of passing it on."""
    payload = {"tasks": [
        {"text": "Migrate DNS", "due": "2026-13-01", "priority": None},
        {"text": "Ship the pack", "due": "2026-07-24", "priority": None},
    ]}
    monkeypatch.setattr(workers, "_worker_client", lambda: fake_client(json.dumps(payload)))
    tasks = asyncio.run(workers.extract_tasks("stuff to do", "2026-07-20"))
    assert tasks[0]["due"] is None
    assert tasks[0]["dropped_due"] == "2026-13-01"  # surfaced so drafters force a checkbox
    assert tasks[1]["due"] == "2026-07-24"
    assert "dropped_due" not in tasks[1]


def capture_prompt(monkeypatch):
    """A fake client that records the one prompt it was sent."""
    captured = {}

    async def create(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"]
        msg = SimpleNamespace(content='{"tasks": []}')
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(workers, "_worker_client", lambda: client)
    return captured


def test_adapt_default_is_hedged_for_the_builtin_worker(monkeypatch):
    # WORKER_ADAPT unset pairs with the built-in 4B default: hedges apply.
    monkeypatch.delenv("WORKER_ADAPT", raising=False)
    captured = capture_prompt(monkeypatch)
    asyncio.run(workers.extract_tasks("x", "2026-07-20"))
    assert "owed_by ONLY when the text names who owes it" in captured["prompt"]
    assert "{hedges}" not in captured["prompt"]


def test_adapt_none_runs_the_loose_base_prompt(monkeypatch):
    monkeypatch.setenv("WORKER_ADAPT", "none")
    captured = capture_prompt(monkeypatch)
    asyncio.run(workers.extract_tasks("x", "2026-07-20"))
    # no hedge rules, no leftover slot — and the capable-model rules stand
    assert "owed_by ONLY when the text names who owes it" not in captured["prompt"]
    assert "{hedges}" not in captured["prompt"]
    assert "dependency is usually TWO items" in captured["prompt"]


def test_hedges_are_extra_rules_not_a_different_contract(monkeypatch):
    # Hedged and loose tiers share the base rules and the reply schema —
    # the adapt layer narrows judgment, it never forks the API.
    monkeypatch.setenv("WORKER_ADAPT", "small")
    captured = capture_prompt(monkeypatch)
    asyncio.run(workers.extract_tasks("x", "2026-07-20"))
    hedged = captured["prompt"]
    assert "dependency is usually TWO items" in hedged  # base rule still there
    assert workers.WORKERS["extract_tasks"].schema is not None


def test_an_inline_hedges_slot_is_filled_not_left_for_format(monkeypatch):
    """The slot has to be gone whichever way a spec wrote it. One left
    behind reaches .format() as KeyError('hedges') — raised inside
    run_worker, outside its VaultError contract, so it takes down every
    pass instead of failing one processor."""
    spec = workers.WorkerSpec(
        name="inline", prompt="Rules: {hedges} about {text}",
        hedges="- be careful", schema={"type": "object"})
    monkeypatch.setenv("WORKER_ADAPT", "small")
    captured = capture_prompt(monkeypatch)
    asyncio.run(workers.run_worker(spec, text="stuff"))
    assert captured["prompt"] == "Rules: - be careful about stuff"

    monkeypatch.setenv("WORKER_ADAPT", "none")
    asyncio.run(workers.run_worker(spec, text="stuff"))
    assert captured["prompt"] == "Rules:  about stuff"


def test_hedges_with_nowhere_to_go_fail_at_import_not_silently():
    """Hedges on a prompt with no slot would simply vanish, and the first
    sign would be a small model quietly running the loose rules."""
    with pytest.raises(ValueError, match="no .* slot"):
        workers.WorkerSpec(name="slotless", prompt="Rules: {text}",
                           hedges="- be careful", schema={"type": "object"})
