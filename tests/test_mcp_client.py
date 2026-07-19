"""Tool-name namespacing: exposed names must be snake_case-safe, and
lookups must tolerate model-side hyphen/underscore mangling."""

import json

from harness.mcp_client import (
    MultiServerClient,
    ToolInfo,
    digest_docs_search,
    sanitize_tool_name,
)


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
