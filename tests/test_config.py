import os

import pytest

from src.config import load_config


def test_load_config_defaults(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DSH_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    cfg = load_config()
    assert cfg.api_key == ""
    assert cfg.model == "deepseek-v4-flash"
    assert cfg.base_url == "https://api.deepseek.com/v1"
    assert cfg.session_root.name == "sessions"


def test_load_config_from_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DSH_MODEL", "deepseek-r1")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("DSH_SESSION_ROOT", "/tmp/my-sessions")
    cfg = load_config()
    assert cfg.api_key == "sk-test"
    assert cfg.model == "deepseek-r1"
    assert cfg.base_url == "http://localhost:8000/v1"
    assert str(cfg.session_root) == "/tmp/my-sessions"


def test_load_config_defaults_to_the_deepseek_backend(monkeypatch):
    monkeypatch.delenv("HARNESS_PROVIDER", raising=False)
    monkeypatch.delenv("HARNESS_CLAUDE_MODEL", raising=False)
    cfg = load_config()
    assert cfg.provider == "deepseek"
    assert cfg.claude_model == "sonnet"
    assert cfg.needs_deepseek_key is True


def test_load_config_reads_the_claude_backend(monkeypatch):
    monkeypatch.setenv("HARNESS_PROVIDER", " Claude ")
    monkeypatch.setenv("HARNESS_CLAUDE_MODEL", "opus")
    cfg = load_config()
    assert cfg.provider == "claude"
    assert cfg.claude_model == "opus"
    # The CLI carries its own credentials, so a missing DeepSeek key is not a
    # reason to refuse to start.
    assert cfg.needs_deepseek_key is False


def test_phase_cfg_resolves_the_model_for_the_active_provider(monkeypatch):
    monkeypatch.setenv("DSH_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("HARNESS_CLAUDE_MODEL", "opus")

    monkeypatch.setenv("HARNESS_PROVIDER", "deepseek")
    assert load_config().phase_cfg()["model"] == "deepseek-v4-flash"

    monkeypatch.setenv("HARNESS_PROVIDER", "claude")
    claude = load_config().phase_cfg()
    assert claude["model"] == "opus"
    assert claude["provider"] == "claude"
