"""service.run_command — the canvas's instant command execution: same
grammar and same kill switch as the pass's command phase, but the
result returns to the caller and the vault is untouched."""

import asyncio

import pytest

from chickenskratch import commands, service


@pytest.fixture()
def vault(gitvault):
    return gitvault


def test_parse_matches_only_known_complete_commands():
    assert commands.parse("//azure alert rules") == ("azure", "alert rules")
    assert commands.parse("//azure") is None          # no args: still typing
    assert commands.parse("// a comment") is None
    assert commands.parse("//research web") is None   # reserved, not registered
    assert commands.parse("plain text") is None


def test_run_command_executes_and_validates(vault, monkeypatch):
    async def fake_azure(args, vault_path):
        return f"result for {args}"

    monkeypatch.setitem(commands.INLINE_COMMANDS, "azure", fake_azure)
    out = asyncio.run(service.run_command(str(vault), "azure", "kql basics"))
    assert out == "result for kql basics"
    with pytest.raises(ValueError):
        asyncio.run(service.run_command(str(vault), "nope", "x"))


def test_run_command_honors_the_spec_kill_switch(vault, monkeypatch):
    async def fake_azure(args, vault_path):
        return "should never run"

    monkeypatch.setitem(commands.INLINE_COMMANDS, "azure", fake_azure)
    (vault / "Harness/Spec.md").write_text(
        "---\ndisabled_processors: [inline_command]\n---\n\n# Vault Spec\n")
    with pytest.raises(ValueError, match="disabled"):
        asyncio.run(service.run_command(str(vault), "azure", "query"))
