"""Tool-name namespacing: exposed names must be snake_case-safe, and
lookups must tolerate model-side hyphen/underscore mangling."""

from harness.mcp_client import MultiServerClient, ToolInfo, sanitize_tool_name


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
