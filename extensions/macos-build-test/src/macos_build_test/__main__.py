"""Entry point for the macOS build/test extension client.

M0: a runnable stub. The MCP server (build/test tools) arrives in M4.
"""

from __future__ import annotations

from . import __version__


def main() -> int:
    print(f"macos-build-test {__version__} — skeleton (M0). MCP server not yet implemented.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
