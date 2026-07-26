"""The dashboard server: JSON view-models over the same collectors the
markdown surfaces use (never markdown itself — the 0-C split rule),
and Phase 2's write verbs behind the hardened consent channel:
JSON-only (forces CORS preflight), local-origin-only, everything
resolving through service.py under the pass lock."""

import asyncio

import pytest
from starlette.testclient import TestClient

from harness import proposals, ui
from harness.watcher import Watcher


@pytest.fixture()
def vault(gitvault):
    return gitvault


@pytest.fixture()
def client(vault, tmp_path, monkeypatch):
    """A server over the fixture vault, queue isolated like the engine
    fixtures — the server derives the queue dir, so dir_for is patched."""
    monkeypatch.setattr("harness.watcher.record_dashboard", lambda *a, **k: None)
    pdir = tmp_path / "proposals"
    monkeypatch.setattr("harness.proposals.dir_for", lambda vp: pdir)
    monkeypatch.setattr("harness.watcher.STATE_PATH", tmp_path / "state.json")
    return TestClient(ui.build_app(str(vault)))


def plant(vault, pdir) -> dict:
    p = proposals.new_proposal(
        kind="schema_fix", note="Home.md", vault_sha="",
        note_content=(vault / "Home.md").read_text(),
        title="Fix Home", summary="s", issues=["i1"],
        apply=[{"tool": "update_frontmatter",
                "args": {"path": "Home.md", "key": "status", "value": "x"}}])
    proposals.save(p, pdir)
    return p


def test_today_is_json_not_markdown(client):
    data = client.get("/api/today").json()
    assert {"date", "due", "waiting", "in_flight", "activity"} <= set(data)
    assert isinstance(data["due"]["overdue"], list)


def test_review_ships_structured_diffs(client, vault):
    plant(vault, proposals.dir_for(str(vault)))
    (row,) = client.get("/api/review").json()
    assert row["id"].startswith("p-") and row["note"] == "Home.md"
    kinds = {r["kind"] for r in row["diff"]}
    assert kinds <= {"context", "change", "skip"} and "change" in kinds
    # rows, not renderings: no HTML in the payload
    assert "<table" not in str(row["diff"])


def test_changes_filters_machine_renders_and_carries_shas(client, vault):
    asyncio.run(Watcher(str(vault)).run_once())  # render surfaces
    data = client.get("/api/changes").json()
    assert all(c["sha"] for c in data["commits"])
    assert not any("Harness/Review.md" in c["files"] and len(c["files"]) == 1
                   for c in data["commits"])


def test_health_reports_worker_and_pending(client, vault):
    data = client.get("/api/health").json()
    assert {"worker", "pending", "head"} <= set(data)
    assert isinstance(data["worker"]["reachable"], bool)
    # no editor-scheme knob rides the health payload any more: nothing in
    # the UI links out to another app for it to configure
    assert "link_scheme" not in data


def test_views_reject_post(client):
    """Reads stay reads: POST on a view route is rejected."""
    for path in ("/api/today", "/api/review", "/api/changes", "/api/health",
                 "/api/tree"):
        assert client.post(path).status_code == 405


def test_tree_renders_the_vault_and_stays_blind_to_the_excluded(client, vault):
    """The file explorer's view-model. It walks the vault through
    iter_notes, so the exclusions are the same ones every other read
    path obeys (I4) — the rail cannot become the one surface that leaks
    Private/."""
    data = client.get("/api/tree").json()
    notes = data["notes"]
    assert "Home.md" in notes and "Tasks/Inbox.md" in notes
    assert not any(n.startswith("Private/") for n in notes)
    assert not any(n.startswith(".") for n in notes)   # .git is not a folder
    assert all(n.endswith(".md") for n in notes)
    # machine surfaces ride along so the rail can ghost them by name
    assert "Harness" in data["surface_dirs"]
    assert data["truncated"] is False
    # a note that appears is a note the next refetch shows: no cached shape
    (vault / "Tasks/brand new.md").write_text("# new\n")
    assert "Tasks/brand new.md" in client.get("/api/tree").json()["notes"]


def test_tree_is_bounded(client, monkeypatch):
    """I6: a pathological vault costs one big response, not an unbounded
    one — and says so instead of silently showing a partial vault."""
    monkeypatch.setattr(ui, "MAX_TREE_NOTES", 3)
    data = client.get("/api/tree").json()
    assert len(data["notes"]) == 3 and data["truncated"] is True


def test_decide_button_applies_and_offers_undo(client, vault):
    p = plant(vault, proposals.dir_for(str(vault)))
    r = client.post("/api/decide", json={"pid": p["id"], "verdict": "approve"})
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["approved"] == 1
    assert len(body["undo"]) >= 1  # the apply's snapshot(s), newest first
    assert all(len(sha) == 40 for sha in body["undo"])
    assert proposals.pending(proposals.dir_for(str(vault))) == []
    # undo IS revert(sha): unwinding through the same gated path
    for sha in body["undo"]:
        rr = client.post("/api/revert", json={"sha": sha})
        assert rr.status_code == 200
    from obsidian_mcp import markdown as md

    fm, _ = md.split_frontmatter((vault / "Home.md").read_text())
    assert fm.get("status") != "x"


def test_decide_rejects_junk(client):
    assert client.post("/api/decide", json={"pid": "p-nope", "verdict": "approve"}).status_code == 400
    assert client.post("/api/decide", json={"pid": "p-" + "a" * 12, "verdict": "shrug"}).status_code == 400


def test_consent_channel_refuses_forms_and_foreign_origins(client, vault):
    """A hostile page's form POST (no preflight) dies on content-type;
    a cross-origin fetch dies on the origin check."""
    p = plant(vault, proposals.dir_for(str(vault)))
    form = client.post("/api/decide", data={"pid": p["id"], "verdict": "approve"})
    assert form.status_code == 415
    foreign = client.post("/api/decide", json={"pid": p["id"], "verdict": "approve"},
                          headers={"Origin": "https://evil.example"})
    assert foreign.status_code == 403
    assert len(proposals.pending(proposals.dir_for(str(vault)))) == 1  # untouched


def test_pass_route_fires_and_forgets(client):
    r = client.post("/api/pass", json={})
    assert r.status_code == 202 and r.json()["started"] is True


def test_task_done_route_and_stale_row(client, vault):
    (vault / "Tasks/Inbox.md").write_text("## Tasks\n\n- [ ] ship the detection pack\n")
    r = client.post("/api/task/done",
                    json={"note": "Tasks/Inbox.md", "text": "ship the detection pack"})
    assert r.status_code == 200
    assert "- [x] ship the detection pack" in (vault / "Tasks/Inbox.md").read_text()
    # a row whose task no longer matches is a 409, never a crash
    r = client.post("/api/task/done",
                    json={"note": "Tasks/Inbox.md", "text": "no such task"})
    assert r.status_code == 409


def test_chat_routes_are_wired_and_guarded(client):
    assert client.post("/api/chat", json={"text": ""}).status_code == 400
    assert client.post("/api/chat", data={"text": "hi"}).status_code == 415
    # unknown confirm id: gone (the chip expired)
    r = client.post("/api/chat/confirm", json={"id": "nope", "allow": True})
    assert r.status_code == 410


def test_canvas_ingest_splits_commands_from_capture(client, vault, monkeypatch):
    """One dump, two classes of change: the //command line executes
    instantly (I3), the prose rides the capture pipeline — here
    auto-trusted extraction files the task and the dump survives in
    git."""
    from harness import commands as cmd_mod

    async def fake_azure(args, vault_path):
        return f"- [Ranked hit for {args}](https://learn.microsoft.com/x)"

    async def fake_extract(text, today):
        assert "renew the domain" in text and "//azure" not in text
        return [{"text": "renew the domain", "due": "2026-08-01"}]

    monkeypatch.setitem(cmd_mod.INLINE_COMMANDS, "azure", fake_azure)
    monkeypatch.setattr("obsidian_mcp.workers.extract_tasks", fake_extract)
    r = client.post("/api/ingest", json={
        "text": "//azure sentinel alert rules\nrenew the domain by friday\n"})
    assert r.status_code == 200
    body = r.json()
    (cmd,) = body["commands"]
    assert cmd["command"] == "azure" and "Ranked hit" in cmd["result"]
    assert body["summary"]["auto"] == 1
    assert "renew the domain" in (vault / "Tasks/Inbox.md").read_text()


def test_canvas_command_only_dump_never_touches_capture(client, vault, monkeypatch):
    from harness import commands as cmd_mod

    async def fake_azure(args, vault_path):
        return "- [hit](https://learn.microsoft.com/y)"

    monkeypatch.setitem(cmd_mod.INLINE_COMMANDS, "azure", fake_azure)
    cap = vault / "Harness/Capture.md"
    before = cap.read_text() if cap.exists() else None
    r = client.post("/api/ingest", json={"text": "//azure defender xdr\n"})
    assert r.status_code == 200 and r.json()["queued"] == []
    after = cap.read_text() if cap.exists() else None
    assert after == before  # a command-only dump runs no capture pass


def test_cancel_route_marks_the_job_and_takes_the_consent_gate(client):
    """The stop button's channel: a write verb, so it takes the same
    JSON-only gate as decide/revert — and it only accepts known jobs, so
    a typo can't silently no-op while the UI says "stopping"."""
    from harness import cancel

    cancel.clear(cancel.PASS)
    try:
        assert client.post("/api/cancel", content="what=pass").status_code == 415
        assert client.post("/api/cancel", json={"what": "bogus"}).status_code == 400
        assert cancel.is_requested(cancel.PASS) is False  # neither armed anything

        r = client.post("/api/cancel", json={"what": "pass"})
        assert r.status_code == 202
        assert r.json() == {"cancelling": "pass"}
        assert cancel.is_requested(cancel.PASS) is True
    finally:
        cancel.clear(cancel.PASS)


def test_cancelling_chat_denies_the_gate_it_is_parked_on(client):
    """A turn stopped while waiting on Allow/Deny must not hang on a
    future nobody will ever resolve — stopping settles it as a deny."""
    import asyncio

    from harness import cancel

    async def scenario():
        session = ui.pane.ChatSession("/tmp/x")
        fut = asyncio.get_running_loop().create_future()
        session.pending["c1"] = fut
        session.stop()
        assert fut.done() and fut.result() is False
        assert cancel.is_requested(cancel.CHAT) is True
        cancel.clear(cancel.CHAT)

    asyncio.run(scenario())


def test_a_busy_engine_409s_the_decision_instead_of_swallowing_it(client, vault, monkeypatch):
    """The owned-UI regression, at the route: a decision that can't take
    the pass lock must come back as a failure the dashboard can act on —
    not a 200 with a summary of nothing, which rendered as "Failed to
    apply" on a row the UI had already marked decided."""
    import fcntl

    from harness import service
    from harness import watcher as watcher_mod
    monkeypatch.setattr(watcher_mod, "DECISION_LOCK_WAIT_S", 0.2)
    monkeypatch.setattr(service, "DECISION_LOCK_WAIT_S", 0.2)
    p = plant(vault, proposals.dir_for(str(vault)))
    # the same per-vault lock file the Watcher derives (bootstrap.vault_digest)
    from harness.bootstrap import vault_digest
    lock_path = watcher_mod.STATE_PATH.with_name(
        f"watcher_state-{vault_digest(str(vault))}.json").with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        r = client.post("/api/decide", json={"pid": p["id"], "verdict": "approve"})
    assert r.status_code == 409
    assert "busy" in r.json()["error"]
    # and the proposal is still there to decide once the engine frees up
    assert [x["id"] for x in proposals.pending(proposals.dir_for(str(vault)))] == [p["id"]]
