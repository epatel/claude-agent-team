"""Configuration and credential loading for the dev lab.

The lab authenticates to Anthropic with a **Claude subscription**, not an API
key (see cards/subscription-auth.md): log in once with the ``claude`` CLI on the
host and the Agent SDK reads the stored login credentials from ``~/.claude``.
No Claude credential goes in ``.env`` — only the GitHub token does (see
cards/deployment.md).

``load_config`` fails fast: it refuses to start if an API key is present (the
key would override subscription auth and silently bill the API) and reports a
missing GitHub token.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# dev-lab/src/dev_lab/config.py -> dev-lab/.env
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

_REQUIRED = ("GITHUB_TOKEN",)

# These outrank the `claude` login credentials in the SDK's auth precedence, so
# if either is set the lab would bill the API instead of the subscription.
_CONFLICTING = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


@dataclass(frozen=True)
class Config:
    github_token: str
    model: str = "claude-opus-4-8"


def load_config() -> Config:
    """Load config from the environment (and ``.env`` if present).

    Raises ``RuntimeError`` if an API key would override subscription auth, or
    if the GitHub token is missing.
    """
    load_dotenv(_ENV_PATH)  # no-op if the file does not exist; never overrides real env vars

    conflicting = [key for key in _CONFLICTING if os.environ.get(key)]
    if conflicting:
        raise RuntimeError(
            f"{', '.join(conflicting)} is set and would override subscription auth "
            "(it takes precedence over the `claude` login credentials and bills the API). "
            "Unset it so the lab uses your Claude subscription."
        )

    missing = [key for key in _REQUIRED if not os.environ.get(key)]
    if missing:
        raise RuntimeError(
            f"Missing required env vars: {', '.join(missing)}. "
            "Copy dev-lab/.env.example to dev-lab/.env and fill it in. "
            "(Claude auth is a one-time `claude` login, not an env var.)"
        )

    return Config(
        github_token=os.environ["GITHUB_TOKEN"],
        model=os.environ.get("MODEL", "claude-opus-4-8"),
    )
