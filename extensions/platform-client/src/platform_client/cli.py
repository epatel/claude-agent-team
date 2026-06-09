"""The standard platform-client entry point: ``<name> serve --host --port``.

Every client serves its MCP tools the same way (HTTP+SSE, see
cards/control-transports.md); only the server's tools differ. ``extension_cli``
builds the ``main`` for a client from its ``build_server`` factory.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

DEFAULT_PORT = 8970

BuildServer = Callable[..., FastMCP]


def extension_cli(
    name: str, build_server: BuildServer, *, default_port: int = DEFAULT_PORT
) -> Callable[[list[str] | None], int]:
    """Return a ``main(argv) -> int`` for a platform client.

    ``build_server`` is the client's factory: ``(*, host, port) -> FastMCP``
    with its capability tools registered.
    """

    def main(argv: list[str] | None = None) -> int:
        parser = argparse.ArgumentParser(
            prog=name, description=f"{name} platform client (MCP server over HTTP+SSE)."
        )
        sub = parser.add_subparsers(dest="command")
        serve_p = sub.add_parser("serve", help="run the MCP server over SSE")
        serve_p.add_argument("--host", default="127.0.0.1")
        serve_p.add_argument("--port", type=int, default=default_port)
        args = parser.parse_args(argv)

        if args.command == "serve":
            build_server(host=args.host, port=args.port).run(transport="sse")
            return 0

        print(f"{name}. Commands: serve (see --help).")
        return 0

    return main
