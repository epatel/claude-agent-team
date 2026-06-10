"""Shared scaffold + runtime for platform clients (cards/extension-clients.md).

A platform client runs on a machine with a capability the lab lacks (e.g. a
macOS host), **dials the lab** over WebSocket, announces its capabilities, and
runs commands in a manifest-synced mirror of a project's working tree:

- ``manifest`` — content-hash tree manifests, deltas, and the changed-files
  report (the code-transport primitive; the lab uses it too).
- ``ClientRuntime`` / ``connect_forever`` — the dial-in runtime behind the
  ``platform-client connect`` CLI.
"""

from . import manifest
from .runtime import ClientRuntime, connect_forever, connect_once

__version__ = "0.3.0"

__all__ = [
    "ClientRuntime",
    "connect_forever",
    "connect_once",
    "manifest",
    "__version__",
]
