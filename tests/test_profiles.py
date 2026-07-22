"""load_config: model profile parsing, including api_key_env resolution."""

import pytest

from harness.profiles import load_config

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


def test_api_key_env_missing_raises_at_load_time(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    path = tmp_path / "models.yaml"
    path.write_text(CLOUD_YAML, encoding="utf-8")
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        load_config(path)
