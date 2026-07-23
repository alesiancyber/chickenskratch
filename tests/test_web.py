"""Web-layer reliability: failed workers resume their chat, bad model
names are rejected before the SSE stream opens, and human judgments
(confirm/report/revert) land in the training corpus."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from harness.chats import ChatStore
from harness.mcp_client import ServerSpec
from harness.profiles import Config, Profile
from harness.web import AgentWorker, Manager, build_app
from obsidian_mcp.vault import Vault


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
                 tool_filter=None, vault="", chat_id=""):
        self.model_name = model_name
        self.chat_id = chat_id
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
    with TestClient(app, base_url="http://127.0.0.1") as tc:
        r = tc.post("/api/chat", json={"message": "hi", "model": "nope"})
        assert r.status_code == 400
        assert "unknown model" in r.json()["error"]
        assert "fake" in r.json()["error"]  # tells the user what is available


def test_cross_origin_and_dns_rebinding_rejected(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr("harness.chats.CHATS_DIR", tmp_path / "chats")
    app = build_app(make_config(str(vault)), servers=[], vault=str(vault))
    with TestClient(app, base_url="http://127.0.0.1") as tc:
        # DNS rebinding: attacker.com resolving to 127.0.0.1 carries its own Host
        assert tc.get("/", headers={"Host": "attacker.example"}).status_code == 403
        # CSRF: a malicious page firing a no-preflight cross-origin POST
        r = tc.post("/api/reset", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        # the app's own origin and plain non-browser clients still work
        assert tc.post("/api/reset", headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200
        assert tc.post("/api/reset").status_code == 200
        assert tc.get("/", headers={"Host": "localhost:8765"}).status_code == 200


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
    with TestClient(app, base_url="http://127.0.0.1") as tc:
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
    with TestClient(app, base_url="http://127.0.0.1") as tc:
        # no active conversation yet: still saves a (near-empty) report
        r = tc.post("/api/report", json={"description": "streaming stalled"})
        assert r.status_code == 200 and r.json()["ok"]
        saved = list((tmp_path / "reports").glob("*.md"))
        assert len(saved) == 1
        assert "streaming stalled" in saved[0].read_text()


def _bare_confirm_worker(chat_id="chat-1", messages=None):
    """AgentWorker with only the attributes _confirm touches — skips
    __init__ (which would spawn the agent task)."""
    w = object.__new__(AgentWorker)
    w.chat_id = chat_id
    w.model_name = "fake"
    w.pending = {}
    w.agent = SimpleNamespace(messages=messages or [{"role": "user", "content": "hi"}])
    w._out = asyncio.Queue()
    return w


def test_confirm_approve_and_deny_record_corpus_labels(monkeypatch):
    captured = []
    monkeypatch.setattr("harness.web.record_confirm", lambda *a, **k: captured.append((a, k)))
    w = _bare_confirm_worker()

    async def scenario(approve):
        task = asyncio.create_task(w._confirm("add_task", {"text": "x"}))
        await asyncio.sleep(0)
        assert w.resolve(next(iter(w.pending)), approve)
        return await task

    assert asyncio.run(scenario(True)) is True
    assert asyncio.run(scenario(False)) is False
    (a1, k1), (a2, k2) = captured
    assert a1[0] == "chat-1" and a1[3] == "add_task" and a1[4] == {"text": "x"}
    assert a1[5] is True and k1["timed_out"] is False
    assert a2[5] is False and k2["timed_out"] is False


def test_confirm_timeout_records_timed_out(monkeypatch):
    captured = []
    monkeypatch.setattr("harness.web.record_confirm", lambda *a, **k: captured.append((a, k)))
    monkeypatch.setattr("harness.web.CONFIRM_TIMEOUT", 0.01)
    w = _bare_confirm_worker()
    assert asyncio.run(w._confirm("add_task", {})) is False
    (a, k) = captured[0]
    assert a[5] is False and k["timed_out"] is True


def test_report_issue_records_corpus_label(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr("harness.bugreports.REPORTS_DIR", tmp_path / "reports")
    captured = []
    monkeypatch.setattr("harness.web.record_bug_report", lambda *a, **k: captured.append(a))
    app = build_app(make_config(str(vault)), servers=[], vault=str(vault))
    with TestClient(app, base_url="http://127.0.0.1") as tc:
        assert tc.post("/api/report", json={"description": "wrong counts"}).json()["ok"]
    # no active conversation: called with an empty trajectory, which the
    # corpus layer no-ops on (test_corpus covers that guard)
    assert captured[0][2] == [] and captured[0][3] == "wrong counts"


def _vault_with_commit(root, chat_id=""):
    root.mkdir()
    v = Vault(root, chat_id=chat_id)
    (root / "note.md").write_text("content\n")
    v.snapshot_after("create note note.md")
    return v.history()[0]["sha"]


def test_revert_records_corpus_label_via_trailer(tmp_path, monkeypatch):
    sha = _vault_with_commit(tmp_path / "vault", chat_id="chat-777")
    monkeypatch.setattr("harness.web.ChatStore", lambda: ChatStore(tmp_path / "chats"))
    captured = []
    monkeypatch.setattr("harness.web.record_revert", lambda *a, **k: captured.append(a))
    ChatStore(tmp_path / "chats").save(
        "chat-777", "fake",
        [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
    )
    app = build_app(make_config(str(tmp_path / "vault")), servers=[], vault=str(tmp_path / "vault"))
    with TestClient(app, base_url="http://127.0.0.1") as tc:
        assert tc.post("/api/revert", json={"sha": sha}).json()["ok"]
    (call,) = captured
    assert call[0] == "chat-777" and call[1] == "fake" and call[3] == sha
    assert "Chat-Id: chat-777" in call[4]


def test_revert_skips_pruned_chat_and_untrailered_commits(tmp_path, monkeypatch):
    sha = _vault_with_commit(tmp_path / "vault", chat_id="chat-gone")  # chat never saved
    monkeypatch.setattr("harness.web.ChatStore", lambda: ChatStore(tmp_path / "chats"))
    captured = []
    monkeypatch.setattr("harness.web.record_revert", lambda *a, **k: captured.append(a))
    app = build_app(make_config(str(tmp_path / "vault")), servers=[], vault=str(tmp_path / "vault"))
    with TestClient(app, base_url="http://127.0.0.1") as tc:
        assert tc.post("/api/revert", json={"sha": sha}).json()["ok"]  # pruned: revert still works
        v = Vault(tmp_path / "vault")  # no chat_id: an untrailered commit
        (tmp_path / "vault" / "plain.md").write_text("x\n")
        v.snapshot_after("create note plain.md")
        plain_sha = v.history()[0]["sha"]
        assert tc.post("/api/revert", json={"sha": plain_sha}).json()["ok"]
    assert captured == []  # neither case produced a corpus row


def test_server_endpoints_refuse_unknown_backends(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr("harness.chats.CHATS_DIR", tmp_path / "chats")
    app = build_app(make_config(str(vault)), servers=[], vault=str(vault))
    with TestClient(app, base_url="http://127.0.0.1") as tc:
        assert tc.get("/api/servers").json() == {"servers": []}  # none configured
        assert tc.post("/api/servers/start", json={"name": "evil"}).status_code == 404
        assert tc.post("/api/servers/stop", json={"name": "evil"}).status_code == 404
