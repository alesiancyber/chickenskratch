"""load_config: model profile parsing, including api_key_env resolution."""

import pytest

from tezcat.profiles import load_config

MINIMAL_YAML = """
default: local-model
vault: .
models:
  local-model:
    model: some-model
    base_url: http://localhost:11434/v1
"""

CLOUD_YAML = """
default: local-model
vault: .
models:
  local-model:
    model: some-model
    base_url: http://localhost:11434/v1
  cloud-model:
    model: gemini-2.5-pro
    base_url: https://generativelanguage.googleapis.com/v1beta/openai/
    api_key_env: GEMINI_API_KEY
"""


def test_default_api_key_unchanged_without_api_key_env(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(MINIMAL_YAML, encoding="utf-8")
    config = load_config(path)
    assert config.profile("local-model").api_key == "ollama"


def test_api_key_env_resolves_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-value")
    path = tmp_path / "models.yaml"
    path.write_text(CLOUD_YAML, encoding="utf-8")
    config = load_config(path)
    assert config.profile("cloud-model").api_key == "test-key-value"
    # unrelated profile is untouched
    assert config.profile("local-model").api_key == "ollama"


def test_api_key_env_resolves_at_use_not_load(tmp_path, monkeypatch):
    # A cloud profile you never select must not demand its key at startup;
    # selecting it without the key fails loud.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    path = tmp_path / "models.yaml"
    path.write_text(CLOUD_YAML, encoding="utf-8")
    config = load_config(path)  # no raise
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        config.profile("cloud-model")
    monkeypatch.setenv("GEMINI_API_KEY", "k-123")
    assert config.profile("cloud-model").api_key == "k-123"


def test_worker_adapt_defaults_loose_and_rides_the_env(tmp_path, monkeypatch):
    """The adapt layer's plumbing: capability is the norm a profile opts
    OUT of (default "none"), a small profile declares "small", and
    resolve_worker_env carries the tier on WORKER_ADAPT like the rest of
    the worker config — explicit env still wins."""
    from tezcat import bootstrap

    path = tmp_path / "models.yaml"
    path.write_text(MINIMAL_YAML + "    worker_adapt: small\nworker: local-model\n",
                    encoding="utf-8")
    config = load_config(path)
    assert config.profile("local-model").worker_adapt == "small"

    for key in ("WORKER_BASE_URL", "WORKER_MODEL", "WORKER_API_KEY", "WORKER_ADAPT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(bootstrap, "_config_filled", set())
    bootstrap.resolve_worker_env(config)
    import os
    assert os.environ["WORKER_ADAPT"] == "small"

    # a user's explicit tier survives the profile fill
    monkeypatch.setenv("WORKER_ADAPT", "none")
    monkeypatch.setattr(bootstrap, "_config_filled", set())
    bootstrap.resolve_worker_env(config)
    assert os.environ["WORKER_ADAPT"] == "none"


def test_undeclared_worker_adapt_reads_none(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(MINIMAL_YAML, encoding="utf-8")
    assert load_config(path).profile("local-model").worker_adapt == "none"
