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

from obsidian_mcp import workers
from obsidian_mcp.vault import VaultError


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
        ]
    }
    monkeypatch.setattr(workers, "_worker_client", lambda: fake_client(json.dumps(payload)))
    tasks = asyncio.run(workers.extract_tasks("Rio needs to migrate DNS by Friday...", "2026-07-20"))
    assert tasks == payload["tasks"]


def test_no_tasks_found(monkeypatch):
    monkeypatch.setattr(workers, "_worker_client", lambda: fake_client('{"tasks": []}'))
    tasks = asyncio.run(workers.extract_tasks("Just a status update, nothing to do.", "2026-07-20"))
    assert tasks == []


def test_drops_entries_missing_text(monkeypatch):
    payload = {"tasks": [{"due": "2026-07-24"}, {"text": "Real task", "due": None, "priority": None}]}
    monkeypatch.setattr(workers, "_worker_client", lambda: fake_client(json.dumps(payload)))
    tasks = asyncio.run(workers.extract_tasks("...", "2026-07-20"))
    assert tasks == [{"text": "Real task", "due": None, "priority": None}]


def test_non_json_output_raises_clean_error(monkeypatch):
    monkeypatch.setattr(
        workers, "_worker_client", lambda: fake_client("sure, here are the tasks:\n- do a thing")
    )
    with pytest.raises(VaultError, match="non-JSON"):
        asyncio.run(workers.extract_tasks("...", "2026-07-20"))


def test_malformed_tasks_field_raises_clean_error(monkeypatch):
    monkeypatch.setattr(workers, "_worker_client", lambda: fake_client('{"tasks": "do a thing"}'))
    with pytest.raises(VaultError, match="malformed"):
        asyncio.run(workers.extract_tasks("...", "2026-07-20"))


def test_worker_unavailable_raises_clean_error(monkeypatch):
    monkeypatch.setattr(
        workers, "_worker_client", lambda: fake_client(raises=ConnectionError("refused"))
    )
    with pytest.raises(VaultError, match="unavailable"):
        asyncio.run(workers.extract_tasks("...", "2026-07-20"))
