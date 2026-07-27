"""Tool-name namespacing: exposed names must be snake_case-safe, and
lookups must tolerate model-side hyphen/underscore mangling."""

import json

from tezcat.mcp_client import (
    MultiServerClient,
    ToolInfo,
    digest_docs_search,
    sanitize_tool_name,
)


def _tool(client, name, server="vault", read_only=False):
    client.tools[name] = ToolInfo(qualified=name, server=server, original=name,
                                  description="", schema={}, read_only=read_only)


def test_all_registered_tools_are_exposed():
    client = MultiServerClient([])
    for n in ("add_task", "list_tasks"):
        _tool(client, n)
    assert {t["function"]["name"] for t in client.openai_tools()} == {"add_task", "list_tasks"}


def test_sanitize_tool_name():
    assert sanitize_tool_name("ms-learn") == "ms_learn"
    assert sanitize_tool_name("already_fine") == "already_fine"


def test_lookup_tolerates_mangled_names():
    client = MultiServerClient([])
    client.tools["ms_learn__docs_search"] = ToolInfo(
        qualified="ms_learn__docs_search", server="ms-learn", original="docs_search",
        description="", schema={}, read_only=True,
    )
    # canonical and mangled variants both resolve to the read-only tool
    assert client.is_write("ms_learn__docs_search") is False
    assert client.is_write("ms-learn__docs_search") is False
    # genuinely unknown stays conservative (treated as write)
    assert client.is_write("nonexistent_tool") is True


def test_docs_search_digest_groups_and_ranks():
    raw = json.dumps({"results": [
        {"title": "Hub-spoke", "contentUrl": "https://x/hub-spoke", "content": "overview text"},
        {"title": "Hub-spoke", "contentUrl": "https://x/hub-spoke#considerations", "content": "..."},
        {"title": "Hub-spoke", "contentUrl": "https://x/hub-spoke#recommendations", "content": "..."},
        {"title": "Private Link", "contentUrl": "https://x/private-link", "content": "pl text"},
    ]})
    digest = digest_docs_search(raw)
    assert digest.count("https://x/hub-spoke") == 1  # one entry per page
    assert "sections: considerations, recommendations" in digest
    assert digest.index("https://x/hub-spoke") < digest.index("https://x/private-link")  # ranked
    assert "covers: overview text" in digest


def test_call_reconnects_a_rotted_session(monkeypatch):
    """A long-lived session can expire under the pane (hours, not the
    CLI's minutes): a raised call reconnects its one server and retries
    once; a second failure is an error RESULT, never an exception."""
    import asyncio
    from contextlib import AsyncExitStack
    from types import SimpleNamespace

    from tezcat.mcp_client import MultiServerClient, ServerSpec, ToolInfo

    spec = ServerSpec(name="docs", transport="http", url="http://x")
    c = MultiServerClient([spec])

    class DeadSession:
        async def call_tool(self, name, args):
            raise RuntimeError("session expired")

    class LiveSession:
        async def call_tool(self, name, args):
            return SimpleNamespace(isError=False,
                                   content=[SimpleNamespace(text="fresh answer")])

    c._sessions["docs"] = DeadSession()
    c._stacks["docs"] = AsyncExitStack()
    c.tools["docs__search"] = ToolInfo(
        qualified="docs__search", server="docs", original="search",
        description="", schema={}, read_only=True)

    async def revive(spec):
        c._sessions["docs"] = LiveSession()
        c._stacks["docs"] = AsyncExitStack()

    monkeypatch.setattr(c, "_connect", revive)
    text, is_error = asyncio.run(c.call("docs__search", {"q": "x"}))
    assert text == "fresh answer" and not is_error

    # and when even the reconnect fails, the turn survives with a message
    async def still_dead(spec):
        raise RuntimeError("network gone")

    c._sessions["docs"] = DeadSession()
    monkeypatch.setattr(c, "_connect", still_dead)
    text, is_error = asyncio.run(c.call("docs__search", {"q": "x"}))
    assert is_error and "unavailable" in text and "tell the user" in text


def test_docs_search_digest_passes_through_non_json():
    assert digest_docs_search("plain text error") == "plain text error"
