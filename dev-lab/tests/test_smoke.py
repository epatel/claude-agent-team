import dev_lab
import pytest
from dev_lab.config import load_config


def _clear_auth_env(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "MODEL", "EXTENSIONS"):
        monkeypatch.delenv(key, raising=False)


def test_version():
    assert dev_lab.__version__


def test_load_config_with_github_token(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    cfg = load_config()

    assert cfg.github_token == "test-token"
    assert cfg.model == "claude-opus-4-8"


def test_load_config_missing_github_token(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        load_config()


def test_load_config_rejects_api_key(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-here")

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        load_config()


def test_load_config_parses_extensions(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("EXTENSIONS", "macos=http://h:1/sse, other=http://h:2/sse")

    cfg = load_config()

    assert cfg.extensions == {"macos": "http://h:1/sse", "other": "http://h:2/sse"}


def test_load_config_no_extensions_is_empty(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    assert load_config().extensions == {}
