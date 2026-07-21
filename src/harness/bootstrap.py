"""Shared startup resolution for every entry point (CLI, web, evals).

One place decides where the config lives, which vault to use, and which
MCP servers to build — the triplication of this logic across entry
points is what produced the cwd-relative vault bug.
"""

from __future__ import annotations

import os
from pathlib import Path

from .mcp_client import ServerSpec, load_mcp_config
from .profiles import Config, load_config

# The harness is a repo-run tool (uv run / editable install): config, skills,
# chats, and eval reports all live in the checkout, anchored here. A
# non-editable install would resolve ROOT into site-packages — if that's ever
# wanted, these anchors must move to config/env instead.
ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DEFAULT_MODELS = CONFIG_DIR / "models.yaml"
DEFAULT_MCP = CONFIG_DIR / "mcp.yaml"


def load_runtime(
    config_path: str | Path | None = None,
    mcp_path: str | Path | None = None,
    vault: str | None = None,
) -> tuple[Config, str, list[ServerSpec]]:
    """Resolve (config, vault, server specs) with standard precedence.

    Vault precedence: explicit arg > $OBSIDIAN_VAULT_PATH > `vault:` in
    models.yaml. Raises SystemExit with a usable message when unset.
    """
    config = load_config(config_path or DEFAULT_MODELS)
    vault = vault or os.environ.get("OBSIDIAN_VAULT_PATH") or config.vault
    if not vault:
        raise SystemExit(
            "No vault configured: pass --vault, set OBSIDIAN_VAULT_PATH, "
            "or set `vault:` in config/models.yaml"
        )
    vault = str(Path(vault).expanduser().resolve())
    servers = load_mcp_config(mcp_path or DEFAULT_MCP, vault)
    return config, vault, servers
