"""Fast unit tests for the harness's core guarantees.

These promises — loop guard, write gate, empty-reply recovery, skill
interception — were previously only exercised through slow model-driven
evals. Here they run in milliseconds against a scripted fake model and a
fake MCP client: no network, no model, no vault.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from harness.agent import Agent
from harness.profiles import Profile
from harness.skills import Skill

# --------------- fakes ---------------

class FakeToolCall:
    def __init__(self, name: str, arguments: str = "{}", call_id: str = "c1"):
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=arguments)

    def model_dump(self):
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


def response(content=None, tool_calls=None, usage=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


def usage(input_tokens, output_tokens):
    return SimpleNamespace(prompt_tokens=input_tokens, completion_tokens=output_tokens)


class FakeOpenAI:
    """Returns scripted responses in order; records what it was sent."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[list[dict]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, model, messages, tools, temperature):
        self.requests.append([dict(m) for m in messages])
        return self._responses.pop(0)


class FakeMCP:
    """Stands in for MultiServerClient: write_* tools are writes, and tools
    prefixed with a name in docs_domains belong to a docs server."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.results: dict[str, str] = {}
        self.docs_domains: dict[str, str] = {"ms_learn": "learn.microsoft.com"}
        self.status = {"fake": {"state": "connected", "tools": 2}}

    def openai_tools(self):
        return [
            {"type": "function", "function": {"name": n, "description": "", "parameters": {}}}
            for n in ("read_thing", "write_thing")
        ]

    def is_write(self, name):
        return not name.startswith("read")

    def docs_domain(self, name):
        prefix = name.split("__")[0]
        return self.docs_domains.get(prefix, "")

    async def call(self, name, args):
        self.calls.append((name, args))
        return self.results.get(name, f"result of {name}"), False


def make_agent(responses, confirm=None, confirm_writes=False, skills=None):
    profile = Profile(name="fake", model="fake", confirm_writes=confirm_writes)
    agent = Agent(profile, servers=[], confirm=confirm, skills=skills or [])
    agent.client = FakeMCP()
    agent._openai = FakeOpenAI(responses)
    agent._tools = agent.client.openai_tools()
    return agent


def run(agent, message="do the thing"):
    return asyncio.run(agent.run(message))


# --------------- loop guard ---------------

def test_loop_guard_blocks_identical_write_in_same_turn():
    agent = make_agent([
        response(tool_calls=[FakeToolCall("write_thing", '{"a": 1}')]),
        response(tool_calls=[FakeToolCall("write_thing", '{"a": 1}', call_id="c2")]),
        response(content="done"),
    ])
    assert run(agent) == "done"
    assert agent.client.calls == [("write_thing", {"a": 1})]  # executed exactly once
    dup_result = [m for m in agent.messages if m["role"] == "tool"][1]["content"]
    assert "ALREADY DONE" in dup_result


def test_loop_guard_allows_different_args_and_resets_per_turn():
    agent = make_agent([
        response(tool_calls=[FakeToolCall("write_thing", '{"a": 1}')]),
        response(tool_calls=[FakeToolCall("write_thing", '{"a": 2}', call_id="c2")]),
        response(content="done"),
        # second turn: same args as turn one must be allowed again
        response(tool_calls=[FakeToolCall("write_thing", '{"a": 1}', call_id="c3")]),
        response(content="done again"),
    ])
    assert run(agent) == "done"
    assert run(agent, "again") == "done again"
    assert len(agent.client.calls) == 3


def test_loop_guard_ignores_reads():
    agent = make_agent([
        response(tool_calls=[FakeToolCall("read_thing")]),
        response(tool_calls=[FakeToolCall("read_thing", call_id="c2")]),
        response(content="done"),
    ])
    run(agent)
    assert len(agent.client.calls) == 2  # reads are never deduplicated


# --------------- write gate ---------------

def test_denied_write_never_executes():
    async def deny(name, args):
        return False

    agent = make_agent(
        [response(tool_calls=[FakeToolCall("write_thing")]), response(content="ok")],
        confirm=deny, confirm_writes=True,
    )
    run(agent)
    assert agent.client.calls == []
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "DECLINED" in tool_msg["content"]


def test_approved_write_executes_and_reads_skip_gate():
    asked = []

    async def approve(name, args):
        asked.append(name)
        return True

    agent = make_agent(
        [
            response(tool_calls=[FakeToolCall("read_thing"), FakeToolCall("write_thing", call_id="c2")]),
            response(content="ok"),
        ],
        confirm=approve, confirm_writes=True,
    )
    run(agent)
    assert asked == ["write_thing"]  # gate consulted for the write only
    assert [c[0] for c in agent.client.calls] == ["read_thing", "write_thing"]


def test_gate_off_never_asks():
    async def bomb(name, args):
        raise AssertionError("confirm must not be called when confirm_writes is off")

    agent = make_agent(
        [response(tool_calls=[FakeToolCall("write_thing")]), response(content="ok")],
        confirm=bomb, confirm_writes=False,
    )
    run(agent)
    assert agent.client.calls == [("write_thing", {})]


def test_override_disables_gate_despite_profile():
    async def bomb(name, args):
        raise AssertionError("gate must not fire when overridden off")

    agent = make_agent(
        [response(tool_calls=[FakeToolCall("write_thing")]), response(content="ok")],
        confirm=bomb, confirm_writes=True,
    )
    agent.confirm_writes_override = False
    run(agent)
    assert agent.client.calls == [("write_thing", {})]


def test_override_enables_gate_despite_profile():
    asked = []

    async def approve(name, args):
        asked.append(name)
        return True

    agent = make_agent(
        [response(tool_calls=[FakeToolCall("write_thing")]), response(content="ok")],
        confirm=approve, confirm_writes=False,
    )
    agent.confirm_writes_override = True
    run(agent)
    assert asked == ["write_thing"]


# --------------- empty-reply recovery ---------------

def test_empty_reply_nudged_and_history_kept_clean():
    agent = make_agent([response(content=""), response(content="recovered")])
    assert run(agent) == "recovered"
    assert all(m["content"] != "" for m in agent.messages if m["role"] == "assistant")


def test_max_iterations_stop_is_recorded_in_history():
    agent = make_agent([
        response(tool_calls=[FakeToolCall("read_thing", call_id=f"c{i}")]) for i in range(3)
    ])
    agent.profile.max_iterations = 3
    result = run(agent)
    assert result.startswith("(stopped")
    assert agent.messages[-1] == {"role": "assistant", "content": result}


def test_double_empty_reply_leaves_no_junk():
    agent = make_agent([response(content=""), response(content="")])
    assert run(agent) == "(no response)"
    roles = [m["role"] for m in agent.messages]
    assert roles == ["system", "user"]  # empty replies and failed nudge all removed


# --------------- docs sources guarantee ---------------

def test_docs_answer_always_carries_source_urls():
    agent = make_agent([
        response(tool_calls=[FakeToolCall("ms_learn__microsoft_docs_search")]),
        response(content="Here is how it works. No links, sorry."),
    ])
    agent.client.results["ms_learn__microsoft_docs_search"] = (
        "- Thing — https://learn.microsoft.com/azure/thing#section\n  covers: x"
    )
    out = run(agent)
    assert "Sources:" in out
    assert "https://learn.microsoft.com/azure/thing" in out  # fragment stripped
    # the guarantee must be persisted, not bolted onto the return value:
    # reopened chats and the model's own context see the same answer
    assert agent.messages[-1]["content"] == out


def test_docs_guarantee_is_config_not_hardcoded():
    # A different docs server (from ServerSpec.docs_domain) gets the same
    # guarantee — nothing in the agent may assume ms-learn specifically.
    agent = make_agent([
        response(tool_calls=[FakeToolCall("py_docs__search")]),
        response(content="Asyncio has tasks and futures."),
    ])
    agent.client.docs_domains = {"py_docs": "docs.python.org"}
    agent.client.results["py_docs__search"] = (
        "- asyncio — https://docs.python.org/3/library/asyncio.html"
    )
    out = run(agent)
    assert "Sources:" in out
    assert "https://docs.python.org/3/library/asyncio.html" in out


def test_system_prompt_rebuilt_each_run():
    # A session living past midnight must not keep yesterday's date: the
    # system prompt is rebuilt at the start of every run.
    agent = make_agent([response(content="hi")])
    agent.messages[0] = {"role": "system", "content": "stale prompt from yesterday"}
    run(agent)
    assert agent.messages[0]["content"] != "stale prompt from yesterday"
    assert "Today is" in agent.messages[0]["content"]


def test_vault_citations_appended_from_reads():
    agent = make_agent([
        response(tool_calls=[FakeToolCall("read_note", '{"path": "Projects/Website Redesign"}')]),
        response(content="The launch moved to Aug 3."),
    ])
    agent.vault = "/vault"
    out = run(agent)
    assert "From your vault:" in out
    assert "Projects/Website Redesign.md" in out
    assert "obsidian://open?path=/vault/Projects/Website%20Redesign.md" in out
    # persisted, not just returned
    assert agent.messages[-1]["content"] == out


def test_vault_citations_include_line_from_search():
    agent = make_agent([
        response(tool_calls=[FakeToolCall("search_vault", '{"query": "dns"}')]),
        response(content="Found a mention of DNS."),
    ])
    agent.vault = "/vault"
    agent.client.results["search_vault"] = json.dumps(
        {"matches": [{"note": "Tasks/Inbox.md", "line": 3, "text": "migrate DNS"}], "total": 1}
    )
    out = run(agent)
    assert "Tasks/Inbox.md (line 3)" in out


def test_no_citations_without_vault():
    # evals construct the Agent without a vault: citations must stay off so
    # answer checks aren't perturbed
    agent = make_agent([
        response(tool_calls=[FakeToolCall("read_note", '{"path": "Projects/X"}')]),
        response(content="done"),
    ])
    assert agent.vault == ""
    out = run(agent)
    assert "From your vault:" not in out


def test_citation_skipped_when_model_already_named_the_note():
    agent = make_agent([
        response(tool_calls=[FakeToolCall("read_note", '{"path": "Tasks/Inbox.md"}')]),
        response(content="See Tasks/Inbox.md — 8 open tasks."),
    ])
    agent.vault = "/vault"
    out = run(agent)
    assert "From your vault:" not in out  # note already referenced inline


def test_no_sources_appended_when_model_already_linked():
    agent = make_agent([
        response(tool_calls=[FakeToolCall("ms_learn__microsoft_docs_search")]),
        response(content="Read https://learn.microsoft.com/azure/thing yourself."),
    ])
    agent.client.results["ms_learn__microsoft_docs_search"] = (
        "- Thing — https://learn.microsoft.com/azure/thing"
    )
    out = run(agent)
    assert "Sources:" not in out  # model included links; no duplication


# --------------- streaming assembly ---------------

def chunk(content=None, tool_deltas=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_deltas)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=usage)


def tool_delta(index=0, call_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index, id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class FakeStreamOpenAI:
    """create(stream=True) yields scripted chunk lists, one list per request."""

    def __init__(self, chunk_lists):
        self._lists = [list(c) for c in chunk_lists]
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, stream=False, **kwargs):
        assert stream, "streaming agent must request stream=True"
        chunks = self._lists.pop(0)

        async def gen():
            for c in chunks:
                yield c

        return gen()


def test_streaming_content_deltas_reach_callback():
    seen = []
    agent = make_agent([])
    agent.on_delta = seen.append
    agent._openai = FakeStreamOpenAI([[chunk("Hel"), chunk("lo"), chunk(" there")]])
    assert run(agent) == "Hello there"
    assert seen == ["Hel", "lo", " there"]


def test_streaming_tool_call_assembled_across_deltas():
    agent = make_agent([])
    agent.on_delta = lambda t: None
    agent._openai = FakeStreamOpenAI([
        [  # tool call split across three deltas
            chunk(tool_deltas=[tool_delta(0, call_id="c9", name="write_thing", arguments='{"a"')]),
            chunk(tool_deltas=[tool_delta(0, arguments=": 1")]),
            chunk(tool_deltas=[tool_delta(0, arguments="}")]),
        ],
        [chunk("done")],
    ])
    assert run(agent) == "done"
    assert agent.client.calls == [("write_thing", {"a": 1})]
    dumped = next(m for m in agent.messages if m.get("tool_calls"))["tool_calls"][0]
    assert dumped["id"] == "c9"
    assert dumped["function"]["arguments"] == '{"a": 1}'


# --------------- token stats ---------------

def test_reported_usage_accumulates_exactly():
    agent = make_agent([
        response(tool_calls=[FakeToolCall("read_thing")], usage=usage(100, 10)),
        response(content="done", usage=usage(150, 5)),
    ])
    run(agent)
    # input/output accumulate across requests; context is the LAST request
    assert agent.stats == {
        "input_tokens": 250, "output_tokens": 15, "context_tokens": 155, "estimated": False,
    }
    assert agent.last_run_stats == {
        "input_tokens": 250, "output_tokens": 15, "tool_calls": 1, "estimated": False,
    }


def test_missing_usage_falls_back_to_estimates_and_is_flagged():
    agent = make_agent([response(content="a short reply about the vault")])
    run(agent)
    assert agent.stats["estimated"] is True
    assert agent.stats["input_tokens"] > 0   # chars/4 of what was sent
    assert agent.stats["output_tokens"] > 0


def test_run_stats_are_per_run_but_chat_stats_accumulate():
    agent = make_agent([
        response(content="one", usage=usage(50, 5)),
        response(content="two", usage=usage(80, 6)),
    ])
    run(agent)
    run(agent, "again")
    assert agent.stats["input_tokens"] == 130
    assert agent.stats["context_tokens"] == 86     # last request's footprint
    assert agent.last_run_stats["input_tokens"] == 80  # second run only


def test_streaming_usage_captured_from_final_chunk():
    agent = make_agent([])
    agent.on_delta = lambda t: None
    agent._openai = FakeStreamOpenAI([[chunk("hi"), chunk(usage=usage(40, 2))]])
    assert run(agent) == "hi"
    assert agent.stats == {
        "input_tokens": 40, "output_tokens": 2, "context_tokens": 42, "estimated": False,
    }


# --------------- skills ---------------

def test_use_skill_returns_body_without_touching_mcp():
    skill = Skill(name="test-skill", description="d", body="THE SECRET PROCEDURE")
    agent = make_agent(
        [
            response(tool_calls=[FakeToolCall("use_skill", '{"name": "test-skill"}')]),
            response(content="ok"),
        ],
        skills=[skill],
    )
    run(agent)
    assert agent.client.calls == []
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "THE SECRET PROCEDURE" in tool_msg["content"]


def test_help_is_deterministic_no_model_call():
    skill = Skill(name="test-skill", description="Does test things. Use for tests.", body="B")
    agent = make_agent([], skills=[skill])  # zero scripted responses: a model call would crash
    out = run(agent, "/help")
    assert "/test-skill" in out and "Does test things" in out
    assert agent.messages[-1]["content"] == out  # recorded in history


def test_unknown_slash_is_deterministic_no_model_call():
    skill = Skill(name="test-skill", description="d", body="B")
    agent = make_agent([], skills=[skill])
    out = run(agent, "/tets-skill do it")
    assert "Unknown command" in out and "/test-skill" in out


def test_slash_command_expands_before_model_sees_it():
    skill = Skill(name="test-skill", description="d", body="THE SECRET PROCEDURE")
    agent = make_agent([response(content="ok")], skills=[skill])
    run(agent, "/test-skill some input")
    sent = agent._openai.requests[0]
    user_msg = next(m for m in sent if m["role"] == "user")
    assert "THE SECRET PROCEDURE" in user_msg["content"]
    assert "some input" in user_msg["content"]
