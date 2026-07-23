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


# WORKER_* keys that were filled from config rather than set by the user
# (shell/.env). Only these may be re-pointed by a Spec.md `worker:` choice —
# precedence stays: explicit env > Spec.md worker > models.yaml worker.
_config_filled: set[str] = set()


def _fill_worker_env(key: str, value: str) -> None:
    if key not in os.environ:
        os.environ[key] = value
        _config_filled.add(key)


def resolve_worker_env(config: Config) -> None:
    """Fill WORKER_* env from the `worker:` profile in models.yaml, unless
    already set — shell/.env always wins. Makes models.yaml the single
    declaration of which model the background workers run on; the env vars
    remain the transport because obsidian_mcp must stay harness-free."""
    if not config.worker:
        return
    prof = config.profile(config.worker)  # fail-loud: a declared worker needs its key
    _fill_worker_env("WORKER_BASE_URL", prof.base_url)
    _fill_worker_env("WORKER_MODEL", prof.model)
    if prof.api_key and prof.api_key != "ollama":
        _fill_worker_env("WORKER_API_KEY", prof.api_key)


def apply_worker_profile(name: str, config: Config | None = None) -> bool:
    """Point the workers at a models.yaml profile by name — the vault's
    Spec.md `worker:` choice, honored once per engine pass. Only env keys
    that config filled are re-pointed (a user's explicit WORKER_MODEL
    still wins), and a bad name warns and keeps the current worker: a
    Spec.md typo must degrade, never break every pass."""
    from obsidian_mcp import workers

    config = config or load_config(DEFAULT_MODELS)
    try:
        prof = config.profile(name)
    except (KeyError, ValueError) as e:
        print(f"Spec.md worker {name!r} unusable, keeping current worker: {e}")
        return False
    changed = False
    for key, value in (
        ("WORKER_BASE_URL", prof.base_url),
        ("WORKER_MODEL", prof.model),
        ("WORKER_API_KEY", prof.api_key),
    ):
        if key in os.environ and key not in _config_filled:
            continue  # user-set: env always wins
        if os.environ.get(key) != value:
            os.environ[key] = value
            changed = True
        _config_filled.add(key)
    if changed:
        workers._client = None  # rebuild against the new endpoint/key
    return True


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
