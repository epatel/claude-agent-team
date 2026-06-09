"""Configuration and credential loading for the dev lab.

The lab authenticates to Anthropic with a **Claude subscription**, not an API
key (see cards/subscription-auth.md): log in once with the ``claude`` CLI on the
host and the Agent SDK reads the stored login credentials from ``~/.claude``.
No Claude credential goes in ``.env``.

GitHub auth is **per project**, not global: each project carries its own token
(stored on its ``projects`` row, entered in the web console) and that token is
used to clone and push that project. There is no global ``GITHUB_TOKEN`` — a
public repo needs no token at all, and different projects can use different
credentials.

``load_config`` fails fast: it refuses to start if an API key is present (the
key would override subscription auth and silently bill the API).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# dev-lab/src/dev_lab/config.py -> dev-lab/.env
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

# These outrank the `claude` login credentials in the SDK's auth precedence, so
# if either is set the lab would bill the API instead of the subscription.
_CONFLICTING = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


# Models offered in the web console. ``id`` is the Anthropic model id passed
# straight to the Agent SDK; ``label`` is what the dropdown shows. A project with
# no model of its own falls back to ``Config.model`` (the lab default).
# Maintained by hand against Anthropic's published lineup — how to check and
# update this list is documented in cards/known-models.md.
KNOWN_MODELS: list[dict[str, str]] = [
    {"id": "claude-fable-5", "label": "Fable 5"},
    {"id": "claude-opus-4-8", "label": "Opus 4.8"},
    {"id": "claude-sonnet-4-6", "label": "Sonnet 4.6"},
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5"},
]
KNOWN_MODEL_IDS = frozenset(m["id"] for m in KNOWN_MODELS)


@dataclass(frozen=True)
class Config:
    model: str = "claude-opus-4-8"
    # name -> MCP SSE URL of an extension client (e.g. macos build/test).
    extensions: dict[str, str] = field(default_factory=dict)


def _parse_extensions(raw: str | None) -> dict[str, str]:
    """Parse EXTENSIONS="name=url,name2=url2" into a dict."""
    result: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise RuntimeError(f"bad EXTENSIONS entry (want name=url): {part!r}")
        name, url = part.split("=", 1)
        result[name.strip()] = url.strip()
    return result


def load_config() -> Config:
    """Load config from the environment (and ``.env`` if present).

    Raises ``RuntimeError`` if an API key would override subscription auth.
    GitHub auth is per project, so there is no required credential env var here.
    """
    load_dotenv(_ENV_PATH)  # no-op if the file does not exist; never overrides real env vars

    conflicting = [key for key in _CONFLICTING if os.environ.get(key)]
    if conflicting:
        raise RuntimeError(
            f"{', '.join(conflicting)} is set and would override subscription auth "
            "(it takes precedence over the `claude` login credentials and bills the API). "
            "Unset it so the lab uses your Claude subscription."
        )

    return Config(
        model=os.environ.get("MODEL", "claude-opus-4-8"),
        extensions=_parse_extensions(os.environ.get("EXTENSIONS")),
    )
