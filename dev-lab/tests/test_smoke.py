import dev_lab
import pytest
from dev_lab.config import load_config


def _clear_auth_env(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "MODEL", "EXTENSIONS", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)


def test_version():
    assert dev_lab.__version__


def test_load_config_defaults(monkeypatch):
    _clear_auth_env(monkeypatch)

    cfg = load_config()

    # GitHub auth is per project now — no global token on Config.
    assert not hasattr(cfg, "github_token")
    assert cfg.model == "claude-opus-4-8"


def test_load_config_no_github_token_required(monkeypatch):
    # The lab must start without any GITHUB_TOKEN (public repos need none).
    _clear_auth_env(monkeypatch)

    cfg = load_config()

    assert cfg.extensions == {}


def test_load_config_rejects_api_key(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-here")

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        load_config()


def test_load_config_parses_extensions(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("EXTENSIONS", "macos=http://h:1/sse, other=http://h:2/sse")

    cfg = load_config()

    assert cfg.extensions == {"macos": "http://h:1/sse", "other": "http://h:2/sse"}


def test_load_config_no_extensions_is_empty(monkeypatch):
    _clear_auth_env(monkeypatch)

    assert load_config().extensions == {}
