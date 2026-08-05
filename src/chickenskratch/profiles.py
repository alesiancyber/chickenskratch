"""Model profiles: everything model-specific lives here, nowhere else.

Swapping models — a bigger local model, a networked vLLM/llama.cpp
server, anything OpenAI-compatible — is a new entry in models.yaml.
Small-model quirks belong in a profile's fields, never in tool code
or the base system prompt.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Profile:
    name: str
    model: str
    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"          # ignored by Ollama, required by the client
    api_key_env: str = ""            # for real providers: name of the env var holding the key
    temperature: float = 0.2
    max_iterations: int = 15         # tool-call rounds before the run is cut off
    max_tool_result_chars: int = 6000
    servers: list[str] | None = None     # MCP server allowlist; None = all enabled
    confirm_writes: bool = False         # gate mutating tool calls on user approval
    max_history_chars: int = 36000       # trim oldest turns beyond this (~9k tokens)
    # Per-request ceiling on ONE chat completion. Load-bearing beyond
    # politeness: a chat turn is only stoppable between model calls, so
    # this number IS the worst-case latency of the pane's stop button
    # (pane.teardown_budget derives from it). Left unset, the OpenAI
    # client's own default is 600s — ten minutes during which a stopped
    # session cannot finish unwinding, which is how the teardown budget
    # kept expiring.
    request_timeout_s: float = 120.0
    # Worker-prompt capability tier when this profile runs the background
    # workers: "none" (default) = the loose capable-model base prompts;
    # "small" = each spec's hedge rules splice in. Capability is the norm
    # a profile has to opt OUT of — small local profiles declare "small".
    worker_adapt: str = "none"
    # Worker reply ceiling when this profile runs the workers; 0 keeps
    # each spec's own bound. Reasoning models spend tokens on hidden
    # thought BEFORE the JSON, so ceilings sized for local workers
    # truncate their replies mid-object.
    worker_max_tokens: int = 0
    # Worker call timeout when this profile runs the workers; 0 keeps
    # each spec's own bound. Model-swapping endpoints (llama-swap) cold
    # start a non-resident model for minutes — a per-endpoint fact, so
    # it lives here, not in every spec's timeout_s.
    worker_timeout_s: float = 0


@dataclass
class BackendSpec:
    """A local model server the UI can start and stop (see models.yaml
    `backends:`). Remote endpoints simply have no backend entry."""
    name: str
    url: str                 # the base_url this backend serves
    command: list[str]       # launch command; the ONLY thing start ever executes
    env: dict = field(default_factory=dict)


@dataclass
class Config:
    default_model: str
    vault: str
    models: dict[str, Profile]
    backends: dict[str, BackendSpec] = field(default_factory=dict)
    worker: str = ""  # profile the background workers run on ("" = env/built-in)

    def profile(self, name: str | None = None) -> Profile:
        key = name or self.default_model
        if key not in self.models:
            raise KeyError(f"No model profile {key!r}. Available: {', '.join(self.models)}")
        profile = self.models[key]
        # api_key_env resolves at use, not load: a cloud profile you never
        # select must not demand its key just to start the chickenskratch.
        if profile.api_key_env:
            env_key = os.environ.get(profile.api_key_env)
            if not env_key:
                raise ValueError(
                    f"Model profile {key!r} sets api_key_env: {profile.api_key_env!r}, "
                    f"but that environment variable is not set."
                )
            profile.api_key = env_key
        return profile


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    models = {name: Profile(name=name, **spec) for name, spec in raw["models"].items()}
    vault = raw.get("vault", "")
    if vault:
        # Relative vault paths are relative to the config file, not the cwd.
        vault = str((path.parent / Path(vault).expanduser()).resolve())
    backends = {}
    for name, spec in (raw.get("backends") or {}).items():
        command = spec.get("command") or ""
        backends[name] = BackendSpec(
            name=name,
            url=str(spec.get("url") or "").rstrip("/"),
            command=shlex.split(command) if isinstance(command, str) else [str(c) for c in command],
            env={k: str(v) for k, v in (spec.get("env") or {}).items()},
        )
    return Config(default_model=raw["default"], vault=vault, models=models,
                  backends=backends, worker=str(raw.get("worker") or ""))
