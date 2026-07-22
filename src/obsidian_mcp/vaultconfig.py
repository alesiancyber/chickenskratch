"""Vault spec loader (DEVPLAN B2): folder→entity-type map, exclusions,
harness surfaces. config/vault.yaml is the one typing source — nothing
else may hardcode a folder→type mapping.

Missing file falls back to safe built-in defaults (no folder map,
Private/ excluded) so the MCP server still runs outside this repo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "vault.yaml"


@dataclass(frozen=True)
class VaultConfig:
    folder_types: dict[str, str]
    exclude: tuple[str, ...]
    surfaces: dict[str, str]
    schemas_dir: Path


def load_config(path: str | Path | None = None) -> VaultConfig:
    p = Path(path or os.environ.get("VAULT_CONFIG_PATH") or _DEFAULT_PATH)
    if not p.exists():
        return VaultConfig(
            folder_types={}, exclude=("Private",), surfaces={}, schemas_dir=p.parent / "schemas"
        )
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return VaultConfig(
        folder_types={str(k): str(v) for k, v in (raw.get("folders") or {}).items()},
        exclude=tuple(str(e) for e in (raw.get("exclude") or [])),
        surfaces={str(k): str(v) for k, v in (raw.get("surfaces") or {}).items()},
        schemas_dir=p.parent / str(raw.get("schemas_dir") or "schemas"),
    )


_config: VaultConfig | None = None


def get_config() -> VaultConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config
