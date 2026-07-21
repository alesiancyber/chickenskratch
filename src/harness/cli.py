"""CLI entry point.

One-shot:     harness "add a task to renew the domain by next friday"
Interactive:  harness -i
Model swap:   harness -m qwen3-8b "..."
Skip gates:   harness --yes "..."   (auto-approve write tools)
"""

from __future__ import annotations

import argparse
import asyncio
import json

from .agent import Agent
from .bootstrap import DEFAULT_MCP, DEFAULT_MODELS, load_runtime
from .skills import load_skills
from .stats import record_run


async def _confirm(name: str, args: dict) -> bool:
    prompt = f"  ⚠ allow {name}({json.dumps(args, ensure_ascii=False)})? [y/N] "
    reply = await asyncio.to_thread(input, prompt)
    return reply.strip().lower() in ("y", "yes")


def main() -> None:
    parser = argparse.ArgumentParser(prog="harness", description="Agent harness for an Obsidian vault")
    parser.add_argument("prompt", nargs="*", help="request to run (omit with -i for a REPL)")
    parser.add_argument("-m", "--model", help="model profile name from models.yaml")
    parser.add_argument("-v", "--vault", help="vault path (default: config, or $OBSIDIAN_VAULT_PATH)")
    parser.add_argument("-c", "--config", default=str(DEFAULT_MODELS), help="path to models.yaml")
    parser.add_argument("--mcp-config", default=str(DEFAULT_MCP), help="path to mcp.yaml")
    parser.add_argument("-i", "--interactive", action="store_true", help="multi-turn REPL")
    parser.add_argument("-y", "--yes", action="store_true", help="auto-approve write tools")
    args = parser.parse_args()

    config, vault, servers = load_runtime(args.config, args.mcp_config, args.vault)
    try:
        profile = config.profile(args.model)
    except KeyError as e:
        parser.error(e.args[0])

    if not args.interactive and not args.prompt:
        parser.error("give a prompt, or use -i for interactive mode")

    print(f"model: {profile.model} @ {profile.base_url}   vault: {vault}")
    asyncio.run(_run(profile, servers, " ".join(args.prompt), args.interactive, args.yes, vault))


async def _run(profile, servers, prompt: str, interactive: bool, auto_yes: bool, vault: str = "") -> None:
    confirm = None if auto_yes else _confirm

    async def run_once(agent, text: str) -> None:
        print(await agent.run(text))
        try:
            record_run(profile.name, agent.last_run_stats)
        except OSError:
            pass  # totals are cosmetic; never fail a run over them

    async with Agent(profile, servers, confirm=confirm, skills=load_skills(), vault=vault) as agent:
        for name, info in agent.client.status.items():
            print(f"  [{name}] {info['state']}, {info['tools']} tools")
        if prompt:
            await run_once(agent, prompt)
        if interactive:
            print("interactive mode — empty line or Ctrl-D to quit")
            while True:
                try:
                    line = input("\n> ").strip()
                except EOFError:
                    break
                if not line:
                    break
                await run_once(agent, line)


if __name__ == "__main__":
    main()
