"""CLI entry point.

One-shot:     harness "add a task to renew the domain by next friday"
Interactive:  tezcat -i
Model swap:   harness -m qwen3-8b "..."
Skip gates:   harness --yes "..."   (auto-approve write tools)

Plus the three verbs that used to be notes in the vault (2026-07-26,
research/surface-subtraction-plan.md). Today.md, Changes.md, and
Review.md were retired as machine-rendered surfaces; these print or act
on exactly the same state, on demand, without a running dashboard:

  tezcat today                     the attention view
  tezcat changes                   recent snapshots
  tezcat decide <pid> approve      consent, headless

That last one matters most: retiring Review.md removed a consent
channel, and consent must never be reachable from only one process.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from .agent import Agent
from .bootstrap import DEFAULT_MCP, DEFAULT_MODELS, load_runtime
from .skills import load_skills
from .stats import record_run

_VERBS = ("today", "changes", "decide")


def _run_verb(verb: str, rest: list[str], vault: str) -> int:
    """The retired surfaces, as commands. Reads (today/changes) go
    straight to the collectors the dashboard uses; `decide` goes through
    service.decide, so it takes the same pass lock and the same _act as
    the button — one decision path, two front doors."""
    from tezcat_vault.vault import Vault
    from tezcat_vault.vaultconfig import get_config

    from . import changes as changes_mod
    from . import proposals, service
    from . import today as today_mod

    cfg = get_config(vault)
    v = Vault(vault, exclude=cfg.exclude)
    if verb == "today":
        pending = len(proposals.pending(proposals.dir_for(vault)))
        print(today_mod.render(v, cfg, pending))
        return 0
    if verb == "changes":
        print(changes_mod.render(v, cfg))
        return 0
    # decide
    if len(rest) != 2 or rest[1] not in ("approve", "reject"):
        print("usage: tezcat decide <proposal-id> approve|reject")
        return 2
    pid, verdict = rest
    try:
        summary = asyncio.run(service.decide(vault, pid, verdict))
    except ValueError as e:
        print(f"error: {e}")
        return 2
    except Exception as e:                      # LockBusy and friends
        print(f"not applied: {e}")
        return 1
    done = summary["approved"] or summary["rejected"]
    past = "approved" if verdict == "approve" else "rejected"
    print(f"{past} {pid}" if done else
          f"nothing applied — {pid} was stale or already resolved")
    return 0 if done else 1


async def _confirm(name: str, args: dict) -> bool:
    prompt = f"  ⚠ allow {name}({json.dumps(args, ensure_ascii=False)})? [y/N] "
    reply = await asyncio.to_thread(input, prompt)
    return reply.strip().lower() in ("y", "yes")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tezcat",
        description="tezcat's chat escape hatch, plus the today/changes/decide verbs")
    parser.add_argument("prompt", nargs="*",
                        help="request to run (omit with -i for a REPL), or one of: "
                             + ", ".join(_VERBS))
    parser.add_argument("-m", "--model", help="model profile name from models.yaml")
    parser.add_argument("-v", "--vault", help="vault path (default: config, or $TEZCAT_VAULT_PATH)")
    parser.add_argument("-c", "--config", default=str(DEFAULT_MODELS), help="path to models.yaml")
    parser.add_argument("--mcp-config", default=str(DEFAULT_MCP), help="path to mcp.yaml")
    parser.add_argument("-i", "--interactive", action="store_true", help="multi-turn REPL")
    parser.add_argument("-y", "--yes", action="store_true", help="auto-approve write tools")
    args = parser.parse_args()

    # The verbs run before any model machinery is touched: `tezcat today`
    # must work with every endpoint down.
    if args.prompt and args.prompt[0] in _VERBS:
        _config, vault, _servers = load_runtime(args.config, args.mcp_config, args.vault)
        raise SystemExit(_run_verb(args.prompt[0], args.prompt[1:], vault))

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
