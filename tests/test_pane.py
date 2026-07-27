"""The escape-hatch pane session: the Agent's hooks pointed at an event
queue — tool rows visible, Allow/Deny resolved by the UI, every gate
verdict a corpus row WITH its trajectory, transcripts scratch."""

import asyncio

from tezcat import pane


class FakeProfile:
    model = "fake-model"
    servers = ["vault"]


class FakeAgent:
    """Agent-shaped: drives the session's hooks the way the real loop
    does — log, then a gated confirm, then a reply."""

    def __init__(self, session):
        self.profile = FakeProfile()
        self.messages = [{"role": "system", "content": "s"}]
        self._s = session
        self.closed = False

    async def run(self, text):
        self.messages.append({"role": "user", "content": text})
        self._s._log("add_task", {"path": "Tasks/Inbox.md", "text": text})
        ok = await self._s._confirm("add_task", {"path": "Tasks/Inbox.md", "text": text})
        if ok:
            self._s._log_result("add_task", {}, '{"updated": "Tasks/Inbox.md"}')
            return "Done — added."
        return "Not executed — nothing changed."

    async def __aexit__(self, *exc):
        self.closed = True


def make_session(vault):
    s = pane.ChatSession(str(vault))

    async def factory():
        return FakeAgent(s)

    s._factory = factory
    return s


def listen(s):
    """Subscribe to the session's fanout — what an SSE stream does. Must
    be called inside the running loop (subscribe records it), and before
    the turn starts: a listener sees what is published after it joins,
    exactly like a browser opening /api/chat/events."""
    return s.events.subscribe()


def drain(queue):
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def test_allow_flows_through_gate_and_lands_in_corpus(vault, tmp_path):
    async def scenario():
        s = make_session(vault)
        q = listen(s)
        turn = asyncio.ensure_future(s.send("add a task to renew the domain"))
        # wait for the gate to arrive
        for _ in range(50):
            await asyncio.sleep(0.01)
            if s.pending:
                break
        (cid,) = s.pending.keys()
        assert s.respond(cid, True)
        await turn
        return s, drain(q)

    s, evs = asyncio.run(scenario())
    types = [e["type"] for e in evs]
    assert types == ["ready", "user", "tool", "confirm_request",
                     "confirm_resolved", "tool_result", "assistant"]
    assert evs[-1]["content"] == "Done — added."
    # the corpus row carries the verdict AND the trajectory
    (row,) = [__import__("json").loads(ln) for f in (tmp_path / "corpus").glob("*.jsonl")
              for ln in f.read_text().splitlines()]
    assert row["label"]["source"] == "confirm" and row["label"]["approved"] is True
    assert row["chat_id"] == s.chat_id and row["messages"]


def test_deny_and_timeout_are_negative_labels(vault, tmp_path, monkeypatch):
    monkeypatch.setattr(pane, "CONFIRM_TIMEOUT", 0.05)

    async def scenario():
        s = make_session(vault)
        q = listen(s)
        await s.send("add something")  # nobody answers -> times out
        return drain(q)

    evs = asyncio.run(scenario())
    resolved = next(e for e in evs if e["type"] == "confirm_resolved")
    assert resolved["approved"] is False
    assert any(e["type"] == "assistant" and "Not executed" in e["content"] for e in evs)
    rows = [__import__("json").loads(ln) for f in (tmp_path / "corpus").glob("*.jsonl")
            for ln in f.read_text().splitlines()]
    assert rows[-1]["label"]["timed_out"] is True and rows[-1]["label"]["approved"] is False


def test_respond_to_unknown_or_settled_id_is_refused(vault):
    s = make_session(vault)
    assert s.respond("deadbeef", True) is False


def test_factory_failure_degrades_to_an_error_event(vault):
    s = pane.ChatSession(str(vault))

    async def broken():
        raise RuntimeError("endpoint down")

    s._factory = broken

    async def scenario():
        q = listen(s)
        await s.send("hi")
        return drain(q)

    evs = asyncio.run(scenario())
    assert evs[0]["type"] == "error" and "endpoint down" in evs[0]["message"]


def test_close_settles_pending_gates_and_exits_agent(vault):
    async def scenario():
        s = make_session(vault)
        q = listen(s)
        turn = asyncio.ensure_future(s.send("add x"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if s.pending:
                break
        agent = s.agent
        await s.close()   # settles the gate as deny
        await turn
        return agent, drain(q)

    agent, evs = asyncio.run(scenario())
    assert agent.closed
    assert any(e["type"] == "confirm_resolved" and e["approved"] is False for e in evs)


def test_two_listeners_each_see_the_whole_turn(vault):
    """The owned-UI regression: the pane used to hand every SSE stream the
    SAME queue, and get() removes the record — so a second tab (or the
    overlap while a browser reconnects) stole half the turn instead of
    receiving a copy of it."""
    async def scenario():
        s = make_session(vault)
        a, b = listen(s), listen(s)
        turn = asyncio.ensure_future(s.send("add x"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if s.pending:
                break
        (cid,) = s.pending.keys()
        s.respond(cid, True)
        await turn
        await asyncio.sleep(0)   # let the threadsafe hand-offs land
        return drain(a), drain(b)

    first, second = asyncio.run(scenario())
    assert [e["type"] for e in first] == [e["type"] for e in second]
    assert "assistant" in [e["type"] for e in first]


def test_a_late_listener_does_not_disturb_the_early_one(vault):
    """Subscribing mid-turn is legal (a reconnect does exactly that) and
    costs the newcomer only what it missed."""
    async def scenario():
        s = make_session(vault)
        early = listen(s)
        turn = asyncio.ensure_future(s.send("add x"))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if s.pending:
                break
        late = listen(s)
        (cid,) = s.pending.keys()
        s.respond(cid, True)
        await turn
        await asyncio.sleep(0)
        return drain(early), drain(late)

    early, late = asyncio.run(scenario())
    assert [e["type"] for e in early][:2] == ["ready", "user"]
    assert [e["type"] for e in late] == ["confirm_resolved", "tool_result", "assistant"]
