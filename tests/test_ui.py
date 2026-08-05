"""The dashboard server: JSON view-models over the same collectors the
markdown surfaces use (never markdown itself — the 0-C split rule),
and Phase 2's write verbs behind the hardened consent channel:
JSON-only (forces CORS preflight), local-origin-only, everything
resolving through service.py under the pass lock."""

import asyncio
import time

import pytest
from starlette.testclient import TestClient

from chickenskratch import proposals, ui
from chickenskratch.watcher import Watcher


@pytest.fixture()
def vault(gitvault):
    return gitvault


@pytest.fixture()
def client(vault, tmp_path, monkeypatch):
    """A server over the fixture vault, queue isolated like the engine
    fixtures — the server derives the queue dir, so dir_for is patched."""
    monkeypatch.setattr("chickenskratch.watcher.record_dashboard", lambda *a, **k: None)
    pdir = tmp_path / "proposals"
    monkeypatch.setattr("chickenskratch.proposals.dir_for", lambda vp: pdir)
    monkeypatch.setattr("chickenskratch.watcher.STATE_PATH", tmp_path / "state.json")
    # base_url matters: every route requires a local Host header (the
    # DNS-rebinding gate), and TestClient's default is "testserver".
    return TestClient(ui.build_app(str(vault)), base_url="http://127.0.0.1")


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
    # machine surfaces are the engine's backend, not the user's tree:
    # filtered out entirely (Capture.md is fed by the canvas, Spec.md
    # edited in the console)
    assert not any(n.startswith("Harness/") for n in notes)
    assert "Harness" in data["surface_dirs"]
    assert data["truncated"] is False
    # a note that appears is a note the next refetch shows: no cached shape
    (vault / "Tasks/brand new.md").write_text("# new\n")
    assert "Tasks/brand new.md" in client.get("/api/tree").json()["notes"]


def test_tree_labels_are_glanceable(client, vault):
    """The rail's display names: the year dropped from dated stems (the
    filename keeps it), and a date-ONLY note — a capture or daily —
    described by its block topics, so "which day did I work detections"
    is answerable by looking instead of searching."""
    (vault / "Meetings" / "2026-07-12 Sentinel Detections.md").write_text("# S\n")
    (vault / "Captures").mkdir(exist_ok=True)
    (vault / "Captures" / "2026-07-15.md").write_text(
        "---\ntype: capture\n---\n\n## 09:10 — sentinel detections\n\nx\n"
        "\n## 14:02 — cloud onboarding\n\ny\n\n## 15:00 — third thing\n\nz\n")
    (vault / "Captures" / "2026-07-16.md").write_text(
        "---\ntype: capture\n---\n\n## 09:10\n\nbare, no topic\n")
    labels = client.get("/api/tree").json()["labels"]
    assert labels["Meetings/2026-07-12 Sentinel Detections.md"] == "07-12 Sentinel Detections"
    # date-only: described by its first topics, capped
    assert labels["Captures/2026-07-15.md"] == "07-15 sentinel detections · cloud onboarding"
    assert labels["Captures/2026-07-16.md"] == "07-16"
    # an undated name needs no label at all
    assert "Home.md" not in labels


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
    from chickenskratch_vault import markdown as md

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


def test_foreign_host_header_is_refused_on_reads_too(client, vault):
    """DNS rebinding resolves an attacker's domain to 127.0.0.1 and reads
    the vault through the victim's browser as same-origin GETs — the
    origin check never fires on a GET, so the Host header is the gate,
    and it guards EVERY route, not just the write verbs."""
    for path in ("/api/today", "/api/tree", "/api/note?path=Home.md"):
        r = client.get(path, headers={"Host": "evil.example"})
        assert r.status_code == 400, path
    r = client.post("/api/decide", json={}, headers={"Host": "evil.example"})
    assert r.status_code == 400


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
    instantly (I3), the prose rides the capture pipeline — here a bare
    task list, fully verifiable, so the record files itself."""
    import datetime as _dt

    from chickenskratch import commands as cmd_mod
    from tests.conftest import record_reply

    async def fake_azure(args, vault_path):
        return f"- [Ranked hit for {args}](https://learn.microsoft.com/x)"

    async def fake_record(text, today):
        assert "renew the domain" in text and "//azure" not in text
        return record_reply([{"text": "renew the domain", "due": "2026-08-01"}])

    monkeypatch.setitem(cmd_mod.INLINE_COMMANDS, "azure", fake_azure)
    monkeypatch.setattr("chickenskratch_vault.workers.extract_record", fake_record)
    r = client.post("/api/ingest", json={
        "text": "//azure sentinel alert rules\nrenew the domain by friday\n"})
    assert r.status_code == 200
    body = r.json()
    (cmd,) = body["commands"]
    assert cmd["command"] == "azure" and "Ranked hit" in cmd["result"]
    # ≥: the full registry runs here, so other processors may also have
    # auto-applied housekeeping in the same pass
    assert body["summary"]["auto"] >= 1
    filed = (vault / "Captures" / f"{_dt.date.today().isoformat()}.md").read_text()
    assert "renew the domain 📅 2026-08-01" in filed


def test_canvas_command_only_dump_never_touches_capture(client, vault, monkeypatch):
    from chickenskratch import commands as cmd_mod

    async def fake_azure(args, vault_path):
        return "- [hit](https://learn.microsoft.com/y)"

    monkeypatch.setitem(cmd_mod.INLINE_COMMANDS, "azure", fake_azure)
    cap = vault / "Harness/Capture.md"
    before = cap.read_text() if cap.exists() else None
    r = client.post("/api/ingest", json={"text": "//azure defender xdr\n"})
    assert r.status_code == 200 and r.json()["queued"] == []
    after = cap.read_text() if cap.exists() else None
    assert after == before  # a command-only dump runs no capture pass


def test_command_route_runs_one_line_and_returns_the_callout(client, monkeypatch):
    """The editor's ⌘↩: same executor and callout as the pass's command
    phase, but the write stays with ⌘S — this route touches no file."""
    from chickenskratch import commands

    async def fake(args, vault_path):
        return "an answer\n\n— [[Tasks/Inbox]]"

    monkeypatch.setitem(commands.INLINE_COMMANDS, "ask", fake)
    r = client.post("/api/command", json={"line": "//ask what matters most?"})
    assert r.status_code == 200
    callout = r.json()["callout"]
    assert callout.startswith("> [!NOTE] //ask what matters most?")
    assert "> an answer" in callout


def test_command_route_rejects_non_commands_and_reports_transients(client, monkeypatch):
    from chickenskratch import commands

    # prose, unknown names, bare commands: 400, named plainly
    assert client.post("/api/command", json={"line": "just a note line"}).status_code == 400
    assert client.post("/api/command", json={"line": "//nope args"}).status_code == 400

    async def down(args, vault_path):
        return None  # the transient contract: worker down, retry is safe

    monkeypatch.setitem(commands.INLINE_COMMANDS, "ask", down)
    r = client.post("/api/command", json={"line": "//ask anything"})
    assert r.status_code == 503 and "try again" in r.json()["error"]


def test_command_route_honors_the_kill_switch(client, vault):
    (vault / "Harness/Spec.md").write_text(
        "---\ndisabled_processors: [inline_command]\n---\n\n# Vault Spec\n")
    r = client.post("/api/command", json={"line": "//ask anything"})
    assert r.status_code == 400 and "disabled" in r.json()["error"]


def test_cancel_route_marks_the_job_and_takes_the_consent_gate(client):
    """The stop button's channel: a write verb, so it takes the same
    JSON-only gate as decide/revert — and it only accepts known jobs, so
    a typo can't silently no-op while the UI says "stopping"."""
    from chickenskratch import cancel

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
    future nobody will ever resolve — stopping settles it as a deny and
    marks the session dead (session-local: the replacement session must
    not be able to clear a stop the old turn depends on)."""
    import asyncio

    async def scenario():
        session = ui.pane.ChatSession("/tmp/x")
        fut = asyncio.get_running_loop().create_future()
        session.pending["c1"] = fut
        session.stop()
        assert fut.done() and fut.result() is False
        assert session.stopped is True
        # a dead session ignores sends — it is already being replaced
        await session.send("hello?")
        assert session.agent is None

    asyncio.run(scenario())


def test_cancelling_chat_replaces_the_session_immediately(client):
    """Stop = kill-and-replace, never a wedged pane: the route swaps in a
    fresh session before returning, tells the old stream to reset, and
    tears the old session down off the request path."""
    before = client.app.state.chat()
    r = client.post("/api/cancel", json={"what": "chat"})
    assert r.status_code == 202 and r.json() == {"cancelling": "chat"}
    after = client.app.state.chat()
    assert after is not before and after.stopped is False
    assert before.stopped is True
    # a second stop keeps working against the new session
    assert client.post("/api/cancel", json={"what": "chat"}).status_code == 202
    assert client.app.state.chat() is not after


def test_console_reads_config_and_gates_its_writes(client, vault):
    """The console's routes. Reads render both homes — vault behaviour and
    repo-side models — and every write takes the same consent gate as the
    rest of the server, because a settings page is a write surface."""
    data = client.get("/api/console").json()
    assert {"vault", "models"} <= set(data)
    assert data["vault"]["values"]["policies"]["capture"] in (
        "off", "ask", "verified", "full")
    assert "capture" in {p["name"] for p in data["vault"]["trustable"]}
    # no secret in the payload, ever — only the env var's name and status
    assert "api_key\"" not in client.get("/api/console").text

    # writes: same channel hardening as decide/revert/ingest
    assert client.post("/api/console/vault", content="n_max=7").status_code == 415
    assert client.post("/api/console/vault", json={"fields": {"n_max": 7}},
                       headers={"Origin": "https://evil.example"}).status_code == 403

    # a rejected setting is a 400 with the reason, not a silent no-op
    r = client.post("/api/console/vault",
                    json={"fields": {"policies": {"capture": "whenever"}}})
    assert r.status_code == 400 and "must be one of" in r.json()["error"]

    levels = dict(data["vault"]["values"]["policies"], capture="full")
    r = client.post("/api/console/vault",
                    json={"fields": {"policies": levels, "n_max": 7}})
    assert r.status_code == 200 and set(r.json()["changed"]) == {"policies", "n_max"}
    after = client.get("/api/console").json()["vault"]["values"]
    assert after["n_max"] == 7 and after["policies"]["capture"] == "full"


def test_task_line_rewrites_without_touching_the_vault(client, vault):
    """The note editor's plan chips. It returns the rewritten line and
    writes NOTHING — that is the point: the editor splices the result into
    an open buffer, so a real write here would stale that buffer and force
    a reload that discards unsaved edits. One implementation of the task
    grammar, in Python, reached over localhost."""
    before = (vault / "Tasks/Inbox.md").read_text()
    line = "- [ ] call the vet ➕ 2026-07-01 ^t-7d2a408e0209"

    r = client.post("/api/task/line", json={"line": line, "when": "2026-08-02"})
    assert r.status_code == 200
    # ⏳ slots BEFORE ➕ (format_task's field order) and the block ref stays last
    assert r.json()["line"] == (
        "- [ ] call the vet ⏳ 2026-08-02 ➕ 2026-07-01 ^t-7d2a408e0209")

    # "" is how the chip's second click clears the plan
    planned = r.json()["line"]
    assert client.post("/api/task/line", json={"line": planned, "when": ""}
                       ).json()["line"] == line

    # a bad date is a 400, never a crash or a corrupted line
    assert client.post("/api/task/line",
                       json={"line": line, "when": "2026-02-30"}).status_code == 400
    # same consent gate as every other POST here
    assert client.post("/api/task/line", content="line=x").status_code == 415
    assert (vault / "Tasks/Inbox.md").read_text() == before   # nothing written


def test_task_line_done_is_the_same_rewrite_as_the_done_verb(client, vault):
    """The editor's checkbox rides {line, done} — tasks.set_line_status,
    the ONE completion rewrite. Completing here must stamp ✅ and
    reopening must strip it, exactly like the dashboard's verb: the
    checkbox's first life as a raw mark flip left editor-completed tasks
    with no done date and reopened ones carrying a stale ✅."""
    import datetime as dt

    line = "- [ ] call the vet ➕ 2026-07-01 ^t-7d2a408e0209"
    done = client.post("/api/task/line",
                       json={"line": line, "done": True}).json()["line"]
    today = dt.date.today().isoformat()
    # mark flipped, ✅ stamped, block ref still the last token
    assert done == f"- [x] call the vet ➕ 2026-07-01 ✅ {today} ^t-7d2a408e0209"

    # reopening strips the stamp cleanly — back to the exact original
    assert client.post("/api/task/line",
                       json={"line": done, "done": False}).json()["line"] == line

    # a non-task line is a 400, never a crash
    assert client.post("/api/task/line",
                       json={"line": "just prose", "done": True}).status_code == 400


def test_shutdown_waits_out_the_sessions_stop_replaced(vault, tmp_path, monkeypatch):
    """The lifespan exists to reap the MCP children a session spawns. Stop
    hands teardown to a background task, so closing only the CURRENT
    session orphans exactly what this hook is for: press stop, then quit,
    and the old session's servers outlive the server."""
    monkeypatch.setattr("chickenskratch.proposals.dir_for", lambda vp: tmp_path / "proposals")
    closed = []

    class SlowAgent:
        profile = type("P", (), {"model": "m", "servers": []})()
        messages: list = []

        async def run(self, text):
            return "ok"

        async def __aexit__(self, *exc):
            await asyncio.sleep(0.05)   # a teardown that takes real time
            closed.append(True)

    app = ui.build_app(str(vault), chat_agent_factory=lambda: _entered(SlowAgent()))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.post("/api/chat", json={"text": "hi"})
        time.sleep(0.2)                 # let the turn build its agent
        assert app.state.chat().agent is not None
        c.post("/api/cancel", json={"what": "chat"})
    # the context manager ran the lifespan's shutdown
    assert closed == [True]


async def _entered(agent):
    return agent


def test_a_chat_stream_releases_the_session_it_subscribed_to(client):
    """A stream binds ONE session for its lifetime. Cleanup that re-read
    the name would unsubscribe from whatever session the stop button
    swapped in — where this queue was never registered — and leave this
    one on the fanout it actually came from.

    The generator is driven by hand: an SSE stream only ends on
    disconnect, and TestClient buffers a response whole, so a plain GET on
    this route never returns."""
    from starlette.requests import Request

    app = client.app
    endpoint = next(r.endpoint for r in app.routes
                    if getattr(r, "path", "") == "/api/chat/events")

    async def scenario():
        before = app.state.chat()
        scope = {"type": "http", "method": "GET", "path": "/api/chat/events",
                 "headers": [], "query_string": b"", "app": app}

        async def receive():
            return {"type": "http.disconnect"}

        response = await endpoint(Request(scope, receive))
        stream = response.body_iterator
        await stream.__anext__()          # the retry hint: we're inside the try now
        assert before.events.listeners == 1

        # the stop button, mid-stream. TestClient drives its own loop, so it
        # cannot be called from inside this one.
        await asyncio.to_thread(lambda: client.post("/api/cancel", json={"what": "chat"}))
        assert app.state.chat() is not before

        await stream.aclose()             # the stream ends, however it ends
        assert before.events.listeners == 0

    asyncio.run(scenario())


def test_a_busy_engine_409s_the_decision_instead_of_swallowing_it(client, vault, monkeypatch):
    """The owned-UI regression, at the route: a decision that can't take
    the pass lock must come back as a failure the dashboard can act on —
    not a 200 with a summary of nothing, which rendered as "Failed to
    apply" on a row the UI had already marked decided."""
    import fcntl

    from chickenskratch import service
    from chickenskratch import watcher as watcher_mod
    monkeypatch.setattr(watcher_mod, "DECISION_LOCK_WAIT_S", 0.2)
    monkeypatch.setattr(service, "DECISION_LOCK_WAIT_S", 0.2)
    p = plant(vault, proposals.dir_for(str(vault)))
    # the same per-vault lock file the Watcher derives (bootstrap.vault_digest)
    from chickenskratch.bootstrap import vault_digest
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


def test_task_due_route_reschedules_through_the_gated_verb(client, vault):
    (vault / "Tasks/Inbox.md").write_text(
        "## Tasks\n\n- [ ] move me 📅 2099-01-10 ^t-aaaaaaaaaaaa\n")
    r = client.post("/api/task/due",
                    json={"note": "Tasks/Inbox.md", "text": "move me", "due": "2099-01-05"})
    assert r.status_code == 200 and r.json()["written"]
    assert "📅 2099-01-05" in (vault / "Tasks/Inbox.md").read_text()
    # a stale row (task gone) is a 409 — refetch, not a crash
    missing = client.post("/api/task/due",
                          json={"note": "Tasks/Inbox.md", "text": "ghost", "due": "2099-01-05"})
    assert missing.status_code == 409


def test_task_plan_route_and_plan_steps_in_review(client, vault):
    """/api/task/plan moves ⏳ only, and a plan_today proposal ships
    per-pick steps so partial approval renders."""
    (vault / "Tasks/Inbox.md").write_text(
        "## Tasks\n\n- [ ] pick me ^t-aaaaaaaaaaaa\n")
    r = client.post("/api/task/plan",
                    json={"note": "Tasks/Inbox.md", "text": "pick me", "when": "2099-01-05"})
    assert r.status_code == 200 and r.json()["written"]
    assert "⏳ 2099-01-05" in (vault / "Tasks/Inbox.md").read_text()

    p = proposals.new_proposal(
        kind="plan_today", note="Tasks/Inbox.md", vault_sha="",
        note_content="x", title="Plan today: 1 task(s)", summary="s",
        issues=["pick me — oldest"],
        apply=[{"tool": "set_task_scheduled",
                "args": {"path": "Tasks/Inbox.md", "task_text": "pick me",
                         "scheduled": "2099-01-05"}}])
    proposals.save(p, proposals.dir_for(str(vault)))
    (row,) = [x for x in client.get("/api/review").json() if x["id"] == p["id"]]
    assert row["steps"][0]["label"] == "pick me"
    assert row["diff"] is None  # judgment payload is the issues list, not a diff


def test_today_ships_the_autonomy_audit_rows(client, vault):
    import datetime as _dt

    p = proposals.new_proposal(
        kind="schema_fix", note="Home.md", vault_sha="", note_content="x",
        title="Fix Home autonomously", summary="s", issues=[],
        apply=[{"tool": "update_frontmatter",
                "args": {"path": "Home.md", "key": "status", "value": "x"}}])
    p["undo"] = ["a" * 40]
    p["verifier_failures"] = ["frontmatter 'status' changed"]
    p["status"] = "auto"
    p["resolved"] = {"ts": _dt.datetime.now().isoformat(timespec="seconds"),
                     "decision": "auto"}
    proposals.save(p, proposals.dir_for(str(vault)))
    (row,) = client.get("/api/today").json()["recent_auto"]
    assert row["title"] == "Fix Home autonomously"
    assert row["undo"] == ["a" * 40] and row["unvouched"] is True


# ---- the AI-layer console routes ----


GOOD_WORKER_YAML = """\
inputs: [text]
prompt: |
  Handle {text}.
schema:
  type: object
  required: [ok]
  properties:
    ok: {type: string}
"""


@pytest.fixture()
def ai_client(vault, tmp_path, monkeypatch):
    """The server plus isolated workers/schemas dirs and models.yaml —
    the AI-layer routes must never write the repo's own config."""
    from chickenskratch import config_store, console
    monkeypatch.setattr("chickenskratch.watcher.record_dashboard", lambda *a, **k: None)
    monkeypatch.setattr("chickenskratch.proposals.dir_for",
                        lambda vp: tmp_path / "proposals")
    monkeypatch.setattr("chickenskratch.watcher.STATE_PATH", tmp_path / "state.json")
    (tmp_path / "workers").mkdir()
    (tmp_path / "schemas").mkdir()
    (tmp_path / "vault.yaml").write_text(
        "workers_dir: workers\nschemas_dir: schemas\n", encoding="utf-8")
    monkeypatch.setenv("VAULT_CONFIG_PATH", str(tmp_path / "vault.yaml"))
    models = tmp_path / "models.yaml"
    models.write_text(
        "default: big\nmodels:\n  big:\n    model: m\n    base_url: http://x/v1\n",
        encoding="utf-8")
    monkeypatch.setattr(console, "MODELS_YAML", models)
    monkeypatch.setattr(config_store, "HISTORY_DIR", tmp_path / "history")
    return TestClient(ui.build_app(str(vault)), base_url="http://127.0.0.1")


def test_ai_routes_take_the_same_consent_gate(ai_client):
    for path in ("/api/console/workers", "/api/console/workers/delete",
                 "/api/console/workers/test", "/api/console/schemas",
                 "/api/console/schemas/delete", "/api/console/models/profile",
                 "/api/console/models/backend"):
        assert ai_client.post(path, content="name=x").status_code == 415, path


def test_worker_save_lands_and_announces_config_changed(ai_client, vault, tmp_path):
    from chickenskratch import events
    r = ai_client.post("/api/console/workers",
                       json={"name": "greeter", "text": GOOD_WORKER_YAML})
    assert r.status_code == 200 and r.json()["restart"] is False
    assert (tmp_path / "workers" / "greeter.yaml").read_text() == GOOD_WORKER_YAML
    logged = events.events_path(str(vault)).read_text()
    assert '"config_changed"' in logged and "workers/greeter.yaml" in logged
    # the console read model now carries it
    names = [w["name"] for w in ai_client.get("/api/console").json()["workers"]["items"]]
    assert "greeter" in names


def test_a_refused_save_is_a_400_with_the_loader_reason(ai_client):
    r = ai_client.post("/api/console/workers",
                       json={"name": "greeter",
                             "text": GOOD_WORKER_YAML.replace("{text}", "{typo}")})
    assert r.status_code == 400 and "undeclared placeholder" in r.json()["error"]


def test_reserved_worker_delete_is_refused_over_http(ai_client):
    r = ai_client.post("/api/console/workers/delete", json={"name": "extract_record"})
    assert r.status_code == 400 and "engine worker" in r.json()["error"]


def test_test_run_round_trips_the_draft(ai_client, monkeypatch):
    import json as _json
    from types import SimpleNamespace

    from chickenskratch import console

    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=_json.dumps({"ok": "hi"})))])

    async def _close():
        return None

    monkeypatch.setattr(
        console.wk, "new_client",
        lambda: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
            close=_close))
    r = ai_client.post("/api/console/workers/test",
                       json={"name": "greeter", "inputs": {"text": "sample"},
                             "text": GOOD_WORKER_YAML})
    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == {"ok": "hi"} and "Handle sample." in body["prompt"]


def test_schema_create_via_route_extends_the_console_model(ai_client, tmp_path):
    # the default kind exists first, as it always does in a real deployment
    (tmp_path / "schemas" / "capture.yaml").write_text(
        "record:\n  kind: capture\n  default: true\n  cues: '- everything else'\n",
        encoding="utf-8")
    r = ai_client.post("/api/console/schemas", json={
        "name": "standup",
        "fields": {"description": "a standup", "id_prefix": "s",
                   "record": {"kind": "standup", "files_as": "note",
                              "dir_key": "meetings_dir",
                              "cues": "- kind \"standup\" for round-ups."},
                   "fields": []}})
    assert r.status_code == 200, r.text
    rows = ai_client.get("/api/console").json()["schemas"]["items"]
    row = next(x for x in rows if x["name"] == "standup")
    assert row["record"]["kind"] == "standup" and row["id_prefix"] == "s"
