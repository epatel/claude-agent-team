"""Shared scaffold + runtime for platform clients (cards/extension-clients.md).

A platform client runs on a machine with a capability the lab lacks (e.g. a
macOS host), **dials the lab** over WebSocket, announces its capabilities, and
runs commands in a manifest-synced mirror of a project's working tree:

- ``manifest`` — content-hash tree manifests, deltas, and the changed-files
  report (the code-transport primitive; the lab uses it too).
- ``ClientRuntime`` / ``connect_forever`` — the dial-in runtime behind the
  ``platform-client connect`` CLI.

Legacy (old MCP-over-SSE model, kept while macos-build-test still uses them):
``run_in_checkout`` / ``CommandResult`` and ``extension_cli``.
"""

from . import manifest
from .cli import extension_cli
from .runtime import ClientRuntime, connect_forever, connect_once
from .workspace import CommandResult, run_in_checkout

__version__ = "0.2.0"

__all__ = [
    "ClientRuntime",
    "CommandResult",
    "connect_forever",
    "connect_once",
    "extension_cli",
    "manifest",
    "run_in_checkout",
    "__version__",
]
