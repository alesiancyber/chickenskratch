"""Tool-name namespacing: exposed names must be snake_case-safe, and
lookups must tolerate model-side hyphen/underscore mangling."""

import json

from harness.mcp_client import (
    MultiServerClient,
    ToolInfo,
    digest_docs_search,
    load_mcp_presets,
    sanitize_tool_name,
)


def _tool(client, name, server="obsidian", read_only=False):
    client.tools[name] = ToolInfo(qualified=name, server=server, original=name,
                                  description="", schema={}, read_only=read_only)


def test_tool_filter_narrows_exposed_tools_but_not_catalog():
    client = MultiServerClient([], tool_filter=["add_task", "list_tasks"])
    for n in ("add_task", "list_tasks", "delete_everything"):
        _tool(client, n)
    exposed = {t["function"]["name"] for t in client.openai_tools()}
    assert exposed == {"add_task", "list_tasks"}  # third tool hidden from the model
    # catalog still lists all tools so the picker can offer them
    assert {t["name"] for t in client.catalog()["obsidian"]} == {"add_task", "list_tasks", "delete_everything"}


def test_no_tool_filter_exposes_everything():
    client = MultiServerClient([])
    _tool(client, "add_task")
    assert len(client.openai_tools()) == 1


def test_load_mcp_presets(tmp_path):
    p = tmp_path / "mcp.yaml"
    p.write_text("servers: {}\npresets:\n  Checklist: [add_task, list_tasks]\n  Notes: [read_note]\n")
    assert load_mcp_presets(p) == {"Checklist": ["add_task", "list_tasks"], "Notes": ["read_note"]}
    p.write_text("servers: {}\n")  # no presets key
    assert load_mcp_presets(p) == {}


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


def test_docs_search_digest_passes_through_non_json():
    assert digest_docs_search("plain text error") == "plain text error"
