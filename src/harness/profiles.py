"""Model profiles: everything model-specific lives here, nowhere else.

Swapping models — a bigger local model, a networked vLLM/llama.cpp
server, anything OpenAI-compatible — is a new entry in models.yaml.
Small-model quirks belong in a profile's fields, never in tool code
or the base system prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class Profile:
    name: str
    model: str
    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"          # ignored by Ollama, required by the client
    temperature: float = 0.2
    max_iterations: int = 15         # tool-call rounds before the run is cut off
    max_tool_result_chars: int = 6000
    parallel_tool_calls: bool = True
    servers: list[str] | None = None     # MCP server allowlist; None = all enabled
    confirm_writes: bool = False         # gate mutating tool calls on user approval
    max_history_chars: int = 36000       # trim oldest turns beyond this (~9k tokens)


@dataclass
class Config:
    default_model: str
    vault: str
    models: dict[str, Profile]

    def profile(self, name: str | None = None) -> Profile:
        key = name or self.default_model
        if key not in self.models:
            raise KeyError(f"No model profile {key!r}. Available: {', '.join(self.models)}")
        return self.models[key]


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    models = {name: Profile(name=name, **spec) for name, spec in raw["models"].items()}
    vault = raw.get("vault", "")
    if vault:
        # Relative vault paths are relative to the config file, not the cwd.
        vault = str((path.parent / Path(vault).expanduser()).resolve())
    return Config(default_model=raw["default"], vault=vault, models=models)
