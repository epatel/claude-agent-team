"""Shared scaffold for platform clients (extension MCP servers).

A platform client runs on a machine with a capability the lab lacks (e.g. a
macOS host) and exposes it as MCP tools over HTTP+SSE (see
cards/extension-clients.md). This package holds the capability-independent
parts so a new client is only its tool definitions:

- ``run_in_checkout`` / ``CommandResult`` — clone a git ref into a throwaway
  workspace, run a command there, return a bounded result.
- ``extension_cli`` — the standard ``<name> serve --host --port`` entry point.
"""

from .cli import extension_cli
from .workspace import CommandResult, run_in_checkout

__version__ = "0.1.0"

__all__ = ["CommandResult", "extension_cli", "run_in_checkout", "__version__"]
