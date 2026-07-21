"""Web-layer reliability: failed workers resume their chat, bad model
names are rejected before the SSE stream opens."""

import asyncio
import json

import pytest
from starlette.testclient import TestClient

from harness.chats import ChatStore
from harness.mcp_client import ServerSpec
from harness.profiles import Config, Profile
from harness.web import Manager, build_app


def make_config(vault: str = "") -> Config:
    return Config(
        default_model="fake",
        vault=vault,
        models={"fake": Profile(name="fake", model="fake")},
    )


class FakeWorker:
    """Stands in for AgentWorker: no MCP, no model, just the manager contract."""

    instances: list = []

    def __init__(self, config, model_name, servers, on_save=None,
                 initial_messages=None, initial_stats=None, server_filter=None,
                 tool_filter=None, vault=""):
        self.model_name = model_name
        self.failed = False
        self.initial_messages = initial_messages
        self.initial_stats = initial_stats
        self.server_filter = server_filter
        self.tool_filter = tool_filter
        self.vault = vault
        FakeWorker.instances.append(self)

    async def stop(self):
        pass


@pytest.fixture()
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr("harness.web.AgentWorker", FakeWorker)
    FakeWorker.instances = []
    return Manager(make_config(), servers=[], store=ChatStore(tmp_path / "chats"))


def test_failed_worker_is_rebuilt_and_resumes_saved_chat(manager):
    async def scenario():
        first = await manager.get_worker(None)
        chat_id = manager.chat_id
        saved = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        manager.store.save(chat_id, "fake", saved)

        first.failed = True  # a run blew up mid-conversation
        second = await manager.get_worker(None)
        assert second is not first  # fresh sessions
        assert manager.chat_id == chat_id  # same conversation
        assert second.initial_messages == saved  # resumed from last save

    asyncio.run(scenario())


def test_healthy_worker_is_reused(manager):
    async def scenario():
        first = await manager.get_worker(None)
        assert await manager.get_worker(None) is first
        assert len(FakeWorker.instances) == 1

    asyncio.run(scenario())


def test_unknown_model_rejected_before_stream(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr("harness.chats.CHATS_DIR", tmp_path / "chats")
    app = build_app(make_config(str(vault)), servers=[], vault=str(vault))
    with TestClient(app) as tc:
        r = tc.post("/api/chat", json={"message": "hi", "model": "nope"})
        assert r.status_code == 400
        assert "unknown model" in r.json()["error"]
        assert "fake" in r.json()["error"]  # tells the user what is available


def _catalog(monkeypatch, tmp_path, cat):
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps(cat))
    monkeypatch.setattr("harness.web.CATALOG_PATH", p)


CAT = {"obsidian": [{"name": "add_task", "read_only": False}, {"name": "list_tasks", "read_only": True}]}


def test_tool_override_derives_servers_rebuilds_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr("harness.web.AgentWorker", FakeWorker)
    FakeWorker.instances = []
    _catalog(monkeypatch, tmp_path, CAT)
    specs = [ServerSpec(name="obsidian", transport="stdio")]
    mgr = Manager(make_config(), servers=specs, store=ChatStore(tmp_path / "chats"))

    async def scenario():
        first = await mgr.get_worker(None)
        assert first.server_filter is None and first.tool_filter is None  # defaults
        chat_id = mgr.chat_id
        mgr.store.save(chat_id, "fake", [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}])

        mgr.tool_override = ["add_task"]           # pin a working set
        second = await mgr.get_worker(None)
        assert second is not first
        assert second.server_filter == ["obsidian"]   # connect only the owning server
        assert second.tool_filter == ["add_task"]     # expose only that tool
        assert mgr.chat_id == chat_id                  # same conversation
        assert second.initial_messages[-1]["content"] == "hi"  # resumed

        assert await mgr.get_worker(None) is second    # unchanged -> reused

    asyncio.run(scenario())


def test_mcp_endpoint_validates_tools(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr("harness.chats.CHATS_DIR", tmp_path / "chats")
    _catalog(monkeypatch, tmp_path, CAT)
    specs = [ServerSpec(name="obsidian", transport="stdio")]
    app = build_app(make_config(str(vault)), servers=specs, vault=str(vault),
                    presets={"Checklist": ["add_task"]})
    with TestClient(app) as tc:
        assert tc.post("/api/mcp", json={"tools": []}).status_code == 400
        assert tc.post("/api/mcp", json={"tools": ["nonexistent"]}).status_code == 400

        r = tc.post("/api/mcp", json={"tools": ["add_task"]})
        assert r.status_code == 200
        obs = next(s for s in r.json()["mcp"]["servers"] if s["name"] == "obsidian")
        sel = {t["name"]: t["selected"] for t in obs["tools"]}
        assert sel["add_task"] and not sel["list_tasks"]

        # presets ride along for the UI
        assert tc.get("/api/config").json()["mcp"]["presets"] == {"Checklist": ["add_task"]}

        # reset to defaults -> everything selected again
        assert tc.post("/api/mcp", json={"tools": None}).status_code == 200
        obs2 = next(s for s in tc.get("/api/config").json()["mcp"]["servers"] if s["name"] == "obsidian")
        assert all(t["selected"] for t in obs2["tools"])


def test_report_issue_saves_conversation(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr("harness.chats.CHATS_DIR", tmp_path / "chats")
    monkeypatch.setattr("harness.bugreports.REPORTS_DIR", tmp_path / "reports")
    app = build_app(make_config(str(vault)), servers=[], vault=str(vault))
    with TestClient(app) as tc:
        # no active conversation yet: still saves a (near-empty) report
        r = tc.post("/api/report", json={"description": "streaming stalled"})
        assert r.status_code == 200 and r.json()["ok"]
        saved = list((tmp_path / "reports").glob("*.md"))
        assert len(saved) == 1
        assert "streaming stalled" in saved[0].read_text()


def test_server_endpoints_refuse_unknown_backends(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr("harness.chats.CHATS_DIR", tmp_path / "chats")
    app = build_app(make_config(str(vault)), servers=[], vault=str(vault))
    with TestClient(app) as tc:
        assert tc.get("/api/servers").json() == {"servers": []}  # none configured
        assert tc.post("/api/servers/start", json={"name": "evil"}).status_code == 404
        assert tc.post("/api/servers/stop", json={"name": "evil"}).status_code == 404
