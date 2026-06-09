"""macOS build/test platform client — the capability layer only.

Everything capability-independent (checkout workspace, serve CLI) comes from
the shared ``platform_client`` scaffold; this module defines just the MCP
tools this host offers the lab.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from platform_client import run_in_checkout


def build_server(*, host: str = "127.0.0.1", port: int = 8970) -> FastMCP:
    mcp = FastMCP("macos-build-test", host=host, port=port)

    @mcp.tool()
    def run_tests(repo: str, ref: str = "HEAD", command: str = "make test") -> dict:
        """Check out a git ref and run the test command on this (macOS) host.

        repo: a git URL or path this host can clone. ref: commit sha or branch.
        command: shell command to run in the checkout (default `make test`).
        """
        return vars(run_in_checkout(repo, ref, command))

    @mcp.tool()
    def build(repo: str, ref: str = "HEAD", command: str = "make build") -> dict:
        """Check out a git ref and run the build command on this (macOS) host."""
        return vars(run_in_checkout(repo, ref, command))

    return mcp
