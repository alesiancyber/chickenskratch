"""Multi-server MCP client: the tool side of the adapter layer.

Connects to every enabled server from config/mcp.yaml over stdio or
streamable HTTP, exposes one merged tool list (namespaced per server),
and routes calls back to the owning session. A server that fails to
connect is reported and skipped — it never takes the session down.

Obsidian tools keep their bare names (primary domain); every other
server's tools are prefixed `server__tool` so origins stay obvious to
both the model and the user.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

PRIMARY_SERVER = "obsidian"
# Kept short: an unreachable remote server (offline laptop) delays the first
# message by this much before degrading gracefully.
CONNECT_TIMEOUT = 6


@dataclass
class ServerSpec:
    name: str
    transport: str                      # "stdio" | "http"
    command: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    url: str = ""
    headers: dict = field(default_factory=dict)
    enabled: bool = True
    assume_read_only: bool = False


def load_mcp_config(path: str | Path, vault: str) -> list[ServerSpec]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    subs = {"{python}": sys.executable, "{vault}": vault}

    def fill(value: str) -> str:
        for key, sub in subs.items():
            value = value.replace(key, sub)
        return value

    specs = []
    for name, spec in (raw.get("servers") or {}).items():
        specs.append(
            ServerSpec(
                name=name,
                transport=spec.get("transport", "stdio"),
                command=[fill(c) for c in spec.get("command", [])],
                env={k: fill(v) for k, v in (spec.get("env") or {}).items()},
                url=fill(spec.get("url", "")),
                headers=spec.get("headers") or {},
                enabled=bool(spec.get("enabled", True)),
                assume_read_only=bool(spec.get("assume_read_only", False)),
            )
        )
    return specs


@dataclass
class ToolInfo:
    qualified: str       # name exposed to the model
    server: str
    original: str        # name on the owning server
    description: str
    schema: dict
    read_only: bool


class MultiServerClient:
    def __init__(self, specs: list[ServerSpec], server_filter: list[str] | None = None):
        if server_filter is not None:
            unknown = set(server_filter) - {s.name for s in specs}
            if unknown:
                raise ValueError(f"Profile references unknown MCP servers: {sorted(unknown)}")
            specs = [s for s in specs if s.name in server_filter]
        self.specs = specs
        self.tools: dict[str, ToolInfo] = {}
        self.status: dict[str, dict] = {}
        self._sessions: dict[str, ClientSession] = {}
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> MultiServerClient:
        for spec in self.specs:
            if not spec.enabled:
                self.status[spec.name] = {"state": "disabled", "tools": 0}
                continue
            try:
                await asyncio.wait_for(self._connect(spec), timeout=CONNECT_TIMEOUT)
            except Exception as e:
                self.status[spec.name] = {"state": f"error: {type(e).__name__}: {e}", "tools": 0}
        if not self._sessions:
            await self._stack.aclose()
            raise RuntimeError(f"No MCP servers available: {self.status}")
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    async def _connect(self, spec: ServerSpec) -> None:
        if spec.transport == "stdio":
            import os

            params = StdioServerParameters(
                command=spec.command[0], args=spec.command[1:], env={**os.environ, **spec.env}
            )
            read, write = await self._stack.enter_async_context(stdio_client(params))
        elif spec.transport == "http":
            read, write, _ = await self._stack.enter_async_context(
                streamablehttp_client(spec.url, headers=spec.headers or None)
            )
        else:
            raise ValueError(f"Unknown transport {spec.transport!r} for server {spec.name!r}")

        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        listed = await session.list_tools()
        self._sessions[spec.name] = session

        for tool in listed.tools:
            qualified = tool.name if spec.name == PRIMARY_SERVER else f"{spec.name}__{tool.name}"
            if qualified in self.tools:
                qualified = f"{spec.name}__{tool.name}"  # collision on primary: prefix anyway
            read_only = spec.assume_read_only or bool(
                tool.annotations and tool.annotations.readOnlyHint
            )
            self.tools[qualified] = ToolInfo(
                qualified=qualified,
                server=spec.name,
                original=tool.name,
                description=tool.description or "",
                schema=tool.inputSchema,
                read_only=read_only,
            )
        self.status[spec.name] = {"state": "connected", "tools": len(listed.tools)}

    def openai_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {"name": t.qualified, "description": t.description, "parameters": t.schema},
            }
            for t in self.tools.values()
        ]

    def is_write(self, qualified: str) -> bool:
        info = self.tools.get(qualified)
        return info is None or not info.read_only

    async def call(self, qualified: str, args: dict) -> tuple[str, bool]:
        """Returns (result text, is_error)."""
        info = self.tools.get(qualified)
        if info is None:
            return f"Unknown tool: {qualified}", True
        result = await self._sessions[info.server].call_tool(info.original, args)
        text = "\n".join(c.text for c in result.content if getattr(c, "text", None)) or "(empty result)"
        return text, bool(result.isError)
