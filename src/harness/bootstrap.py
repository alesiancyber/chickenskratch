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
ENV_FILE = ROOT / ".env"


def load_env_file(path: Path = ENV_FILE) -> None:
    """Minimal .env loader: KEY=value per line, tolerant of spaces around
    `=`, quotes, blank lines, and # comments. Exported shell variables
    always win — the file only fills gaps, so `WORKER_MODEL=x uv run …`
    overrides still work. Loaded before config so api_key_env resolution
    and worker env reads both see it; spawned MCP servers inherit it."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def resolve_worker_env(config: Config) -> None:
    """Fill WORKER_* env from the `worker:` profile in models.yaml, unless
    already set — shell/.env always wins. Makes models.yaml the single
    declaration of which model the background workers run on; the env vars
    remain the transport because obsidian_mcp must stay harness-free."""
    if not config.worker:
        return
    prof = config.profile(config.worker)  # fail-loud: a declared worker needs its key
    os.environ.setdefault("WORKER_BASE_URL", prof.base_url)
    os.environ.setdefault("WORKER_MODEL", prof.model)
    if prof.api_key and prof.api_key != "ollama":
        os.environ.setdefault("WORKER_API_KEY", prof.api_key)


def load_runtime(
    config_path: str | Path | None = None,
    mcp_path: str | Path | None = None,
    vault: str | None = None,
) -> tuple[Config, str, list[ServerSpec]]:
    """Resolve (config, vault, server specs) with standard precedence.

    Vault precedence: explicit arg > $OBSIDIAN_VAULT_PATH > `vault:` in
    models.yaml. Raises SystemExit with a usable message when unset.
    """
    load_env_file()
    config = load_config(config_path or DEFAULT_MODELS)
    resolve_worker_env(config)
    vault = vault or os.environ.get("OBSIDIAN_VAULT_PATH") or config.vault
    if not vault:
        raise SystemExit(
            "No vault configured: pass --vault, set OBSIDIAN_VAULT_PATH, "
            "or set `vault:` in config/models.yaml"
        )
    vault = str(Path(vault).expanduser().resolve())
    servers = load_mcp_config(mcp_path or DEFAULT_MCP, vault)
    return config, vault, servers
