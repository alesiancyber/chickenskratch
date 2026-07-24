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
import json
import os
import re
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

PRIMARY_SERVER = "obsidian"


def sanitize_tool_name(name: str) -> str:
    """Models reliably emit snake_case tool names and mangle anything else
    (a hyphenated prefix like ms-learn__ gets 'corrected' to ms_learn__),
    so exposed names must contain only [A-Za-z0-9_]."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


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
    # Marks a documentation source: results are digested into ranked link
    # lists, and answers built on them must cite URLs from this domain
    # (the agent enforces both — see mcp.yaml).
    docs_domain: str = ""


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
                docs_domain=str(spec.get("docs_domain", "")),
            )
        )
    return specs


def digest_docs_search(text: str) -> str:
    """Deterministically reshape a docs server's JSON search results into a
    ranked link digest: one entry per page (fragments grouped as sections),
    ordered by how many chunks each page contributed. The user wants URLs
    they can read themselves, not model paraphrase — so the harness makes
    links the dominant content the model sees, instead of hoping it
    summarizes less. Non-JSON or unrecognized shapes pass through unchanged."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    results = data if isinstance(data, list) else data.get("results")
    if not isinstance(results, list):
        return text
    pages: dict[str, dict] = {}
    for r in results:
        if not isinstance(r, dict):
            continue
        url = str(r.get("contentUrl") or r.get("url") or "").strip()
        if not url:
            continue
        base, _, fragment = url.partition("#")
        # Excerpt: body prose only. Chunks open with their own headings
        # (the visible title again) and may carry markdown tables that
        # flatten into "| --- |" junk — both line types are dropped. The
        # cut prefers a sentence boundary so an excerpt ends as a complete
        # thought; only when no sentence fits does it fall back to a word
        # boundary with an ellipsis.
        flat = " ".join(" ".join(
            ln.strip() for ln in str(r.get("content") or "").splitlines()
            if ln.strip() and not ln.lstrip().startswith(("#", "|"))
        ).split())
        if len(flat) > 200:
            cut = flat[:200]
            dot = cut.rfind(". ")
            flat = cut[: dot + 1] if dot >= 60 else cut.rsplit(" ", 1)[0] + " …"
        excerpt = flat
        page = pages.setdefault(
            base,
            {"title": str(r.get("title") or base), "sections": [], "count": 0,
             "excerpt": excerpt},
        )
        page["count"] += 1
        if fragment and fragment not in page["sections"]:
            page["sections"].append(fragment)
    if not pages:
        return text
    lines = ["Documentation pages found, ranked by relevance. Give the user these URLs verbatim:"]
    for base in sorted(pages, key=lambda b: -pages[b]["count"]):
        p = pages[base]
        sections = f" (sections: {', '.join(p['sections'])})" if p["sections"] else ""
        lines.append(f"- {p['title']} — {base}{sections}")
        if p["excerpt"]:
            lines.append(f"  covers: {p['excerpt']}")
    return "\n".join(lines)


@dataclass
class ToolInfo:
    qualified: str       # name exposed to the model
    server: str
    original: str        # name on the owning server
    description: str
    schema: dict
    read_only: bool
    docs_domain: str = ""  # inherited from the owning server's spec


class MultiServerClient:
    def __init__(
        self,
        specs: list[ServerSpec],
        server_filter: list[str] | None = None,
    ):
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

        prefix = sanitize_tool_name(spec.name)
        skipped = []
        for tool in listed.tools:
            qualified = tool.name if spec.name == PRIMARY_SERVER else f"{prefix}__{tool.name}"
            if qualified in self.tools:
                qualified = f"{prefix}__{tool.name}"  # collision on primary: prefix anyway
            if qualified in self.tools:
                # Still taken (e.g. two server names that sanitize identically).
                # Never silently shadow a registered tool — skip and report.
                skipped.append(tool.name)
                continue
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
                docs_domain=spec.docs_domain,
            )
        self.status[spec.name] = {
            "state": "connected",
            "tools": len(listed.tools) - len(skipped),
            **({"skipped_name_collisions": skipped} if skipped else {}),
        }

    def openai_tools(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {"name": t.qualified, "description": t.description, "parameters": t.schema},
            }
            for t in self.tools.values()
        ]

    def _lookup(self, qualified: str) -> ToolInfo | None:
        # tolerate model-side hyphen/underscore mangling in either direction
        return self.tools.get(qualified) or self.tools.get(sanitize_tool_name(qualified))

    def is_known(self, qualified: str) -> bool:
        return self._lookup(qualified) is not None

    def is_write(self, qualified: str) -> bool:
        info = self._lookup(qualified)
        return info is None or not info.read_only

    def docs_domain(self, qualified: str) -> str:
        """The owning server's docs domain, or "" for non-docs tools."""
        info = self._lookup(qualified)
        return info.docs_domain if info else ""

    async def call(self, qualified: str, args: dict) -> tuple[str, bool]:
        """Returns (result text, is_error)."""
        info = self._lookup(qualified)
        if info is None:
            known = ", ".join(sorted(self.tools))
            return f"Unknown tool: {qualified}. Available tools: {known}", True
        result = await self._sessions[info.server].call_tool(info.original, args)
        text = "\n".join(c.text for c in result.content if getattr(c, "text", None)) or "(empty result)"
        if info.docs_domain and not result.isError:
            text = digest_docs_search(text)
        return text, bool(result.isError)
