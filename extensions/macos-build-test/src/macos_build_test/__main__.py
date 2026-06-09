"""Entry point for the macOS build/test platform client.

  macos-build-test serve --host 0.0.0.0 --port 8970   # MCP server over HTTP+SSE
"""

from __future__ import annotations

from platform_client import extension_cli

from .server import build_server

main = extension_cli("macos-build-test", build_server, default_port=8970)

if __name__ == "__main__":
    raise SystemExit(main())
