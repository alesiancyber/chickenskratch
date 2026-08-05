"""Cooperative cancellation: the stop button's contract.

The guarantee is not "stops instantly" — it's "stops at a point where
the vault and the model's message history are both consistent". These
pin that: a stopped pass drafts no further and leaves no half-state, a
stopped agent turn still answers every tool_call it announced (an
unanswered one makes the NEXT request malformed), and a stale stop
never kills the job the user just asked for.
"""

import asyncio

import pytest

from chickenskratch import cancel, proposals, service
from chickenskratch.agent import CANCELLED_TEXT, CANCELLED_TOOL_TEXT
from chickenskratch.watcher import Watcher


@pytest.fixture(autouse=True)
def clean_flags():
    for job in cancel.JOBS:
        cancel.clear(job)
    yield
    for job in cancel.JOBS:
        cancel.clear(job)


@pytest.fixture()
def wkw(tmp_path, monkeypatch):
    monkeypatch.setattr("chickenskratch.watcher.record_dashboard", lambda *a, **k: None)
    return {"proposals_dir": tmp_path / "proposals", "state_path": tmp_path / "state.json"}


# ---- the engine ----


def test_cancelled_pass_drafts_nothing_further(gitvault, wkw):
    """Stop before the first draft: the pass completes cleanly, queues
    nothing, and says so — it does not raise, and it does not leave the
    pass lock held."""
    drafted = []

    async def counting_drafter(item):
        drafted.append(item["note"])
        return {"title": "t", "summary": "s", "apply": []}

    w = Watcher(str(gitvault), drafter=counting_drafter,
                should_cancel=lambda: True, **wkw)
    summary = asyncio.run(w.run_once())
    assert summary["cancelled"] is True
    assert summary["drafted"] == 0
    assert drafted == []
    assert proposals.pending(wkw["proposals_dir"]) == []


def test_uncancelled_pass_is_unaffected(gitvault, wkw):
    """The flag defaults off — the stop plumbing must not change the
    behaviour of a normal pass."""
    w = Watcher(str(gitvault), **wkw)
    summary = asyncio.run(w.run_once())
    assert summary["cancelled"] is False


def test_run_pass_clears_a_stale_stop(gitvault, wkw):
    """A stop left over from a previous job must never kill the pass the
    user just asked for."""
    cancel.request(cancel.PASS)
    summary = asyncio.run(service.run_pass(str(gitvault), **wkw))
    assert summary["cancelled"] is False
    assert cancel.is_requested(cancel.PASS) is False


# ---- the tokens ----


def test_token_reflects_live_state():
    tok = cancel.token(cancel.PASS)
    assert tok() is False
    cancel.request(cancel.PASS)
    assert tok() is True          # the same token sees the later request
    cancel.clear(cancel.PASS)
    assert tok() is False


def test_the_pass_is_the_only_job_here():
    """Stopping a chat replaces the session (ui.cancel_route) — its flag
    lives on the ChatSession it kills, so a second job name here would
    imply a symmetry the two verbs do not have."""
    assert cancel.JOBS == (cancel.PASS,)
    assert not hasattr(cancel, "CHAT")


# ---- the agent loop ----


def _agent(responses, should_cancel):
    """Same scripted fakes test_agent_guarantees uses, with a stop token."""
    from chickenskratch.agent import Agent
    from chickenskratch.profiles import Profile
    from tests.test_agent_guarantees import FakeMCP, FakeOpenAI

    agent = Agent(Profile(name="fake", model="fake"), servers=[],
                  should_cancel=should_cancel)
    agent.client = FakeMCP()
    agent._openai = FakeOpenAI(responses)
    agent._tools = agent.client.openai_tools()
    return agent


def test_stopped_turn_answers_every_tool_call_it_announced():
    """The subtle one: an assistant message announcing N tool_calls needs
    N tool replies, or the next request is malformed and the whole
    session is poisoned. A stop mid-batch reports the rest as
    not-executed instead of dropping them."""
    from tests.test_agent_guarantees import FakeToolCall, response

    stop = {"now": False}

    def should_cancel():
        return stop["now"]

    agent = _agent([
        response(tool_calls=[FakeToolCall("read_thing", "{}", "c1"),
                             FakeToolCall("write_thing", '{"a": 1}', "c2")]),
    ], should_cancel)

    # trip the stop after the first tool result comes back
    real_execute = agent._execute

    async def execute_then_stop(call):
        out = await real_execute(call)
        stop["now"] = True
        return out

    agent._execute = execute_then_stop

    assert asyncio.run(agent.run("go")) == CANCELLED_TEXT
    announced = [m for m in agent.messages if m.get("tool_calls")][0]["tool_calls"]
    answered = [m for m in agent.messages if m["role"] == "tool"]
    assert len(answered) == len(announced) == 2
    assert answered[1]["content"] == CANCELLED_TOOL_TEXT
    assert agent.client.calls == [("read_thing", {})]  # the write never ran
    assert agent.messages[-1]["content"] == CANCELLED_TEXT


def test_stop_before_the_first_call_makes_no_model_request():
    agent = _agent([], lambda: True)
    assert asyncio.run(agent.run("go")) == CANCELLED_TEXT
    assert agent._openai.requests == []
