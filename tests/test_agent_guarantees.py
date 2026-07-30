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

from chickenskratch.agent import Agent
from chickenskratch.profiles import Profile
from chickenskratch.skills import Skill

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

    def is_known(self, name):
        return True

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


def test_read_guard_blocks_identical_read_in_same_turn():
    """The 2026-07-25 pane flail: identical reads re-issued against an
    unchanged world get the cached answer, not a re-execution."""
    agent = make_agent([
        response(tool_calls=[FakeToolCall("read_thing")]),
        response(tool_calls=[FakeToolCall("read_thing", call_id="c2")]),
        response(content="done"),
    ])
    run(agent)
    assert len(agent.client.calls) == 1  # executed exactly once
    dup = [m for m in agent.messages if m["role"] == "tool"][1]["content"]
    assert "REPEAT CALL" in dup


def test_read_guard_resets_after_a_write():
    """read -> write -> same read must re-execute: verifying your own
    change is legitimate; the write invalidated the cache."""
    agent = make_agent([
        response(tool_calls=[FakeToolCall("read_thing")]),
        response(tool_calls=[FakeToolCall("write_thing", '{"a": 1}', call_id="c2")]),
        response(tool_calls=[FakeToolCall("read_thing", call_id="c3")]),
        response(content="done"),
    ])
    run(agent)
    reads = [c for c in agent.client.calls if c[0] == "read_thing"]
    assert len(reads) == 2  # both executed


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


def test_unknown_tool_never_prompts_for_approval():
    """A hallucinated tool name can't write anything — the user must never
    be asked to approve a call that cannot execute; the client's
    unknown-tool error goes straight back to the model."""
    async def bomb(name, args):
        raise AssertionError("confirm must not be called for an unknown tool")

    agent = make_agent(
        [response(tool_calls=[FakeToolCall("no_such_tool")]), response(content="ok")],
        confirm=bomb, confirm_writes=True,
    )
    agent.client.is_known = lambda name: False
    assert run(agent) == "ok"
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert tool_msg["content"].startswith("Error:")


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
    # a path, not a launcher: no consumer of this answer opens another app
    assert "://" not in out
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


# --------------- token stats ---------------

def test_reported_usage_accumulates_exactly():
    agent = make_agent([
        response(tool_calls=[FakeToolCall("read_thing")], usage=usage(100, 10)),
        response(content="done", usage=usage(150, 5)),
    ])
    run(agent)
    # input/output accumulate across requests; context is the LAST request
    assert agent.stats == {
        "input_tokens": 250, "output_tokens": 15, "estimated": False,
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
    assert agent.last_run_stats["input_tokens"] == 80  # second run only


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


def test_vault_citations_never_launch_another_editor():
    """There is no link_scheme any more (2026-07-26). A citation is a path
    the consumer decides what to do with — it is never a URL that hands
    the user's own note to Obsidian, VSCode, or anything else."""
    agent = make_agent([
        response(tool_calls=[FakeToolCall("read_note", '{"path": "Projects/Website Redesign"}')]),
        response(content="The launch moved to Aug 3."),
    ])
    agent.vault = "/vault"
    out = run(agent)
    assert "obsidian://" not in out and "vscode://" not in out and "file://" not in out


# --------------- vault citations: where a source opens ---------------

def test_cli_citations_are_plain_paths(tmp_path):
    """The CLI has no editor of its own — so a citation is the vault path
    and the line, printed. Never an app-scheme URL: a terminal that
    launches another app to show you your own note is treating that app
    as the surface, which chickenskratch does not do."""
    from chickenskratch.agent import Agent
    from chickenskratch.profiles import Profile

    agent = Agent(Profile(name="f", model="f"), servers=[], vault=str(tmp_path))
    agent._cited = {"Meetings/Q3.md": {14}}
    out = agent._with_vault_citations("the answer")
    assert "- Meetings/Q3.md (line 14)" in out
    assert "://" not in out


def test_pane_citations_are_vault_links_carrying_the_line():
    """The pane owns an editor, so a source must open THERE — and on the
    line the answer actually drew from, not at the top of the note."""
    from chickenskratch.agent import Agent, app_note_link
    from chickenskratch.profiles import Profile

    agent = Agent(Profile(name="f", model="f"), servers=[], vault="/v",
                  note_link=app_note_link)
    agent._cited = {"Meetings/Q3 Planning.md": {14, 22}}
    out = agent._with_vault_citations("the answer")
    assert "(vault:Meetings/Q3%20Planning.md#14)" in out
    assert "obsidian://" not in out and "vscode://" not in out


def test_citations_still_skip_notes_the_model_already_named():
    from chickenskratch.agent import Agent, app_note_link
    from chickenskratch.profiles import Profile

    agent = Agent(Profile(name="f", model="f"), servers=[], vault="/v",
                  note_link=app_note_link)
    agent._cited = {"Home.md": set()}
    assert agent._with_vault_citations("I read Home.md for this") == "I read Home.md for this"
