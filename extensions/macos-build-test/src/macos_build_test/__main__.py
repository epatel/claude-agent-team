"""Entry point for the macOS build/test extension client.

  macos-build-test serve --host 0.0.0.0 --port 8970   # MCP server over HTTP+SSE
"""

from __future__ import annotations

import argparse

from . import __version__
from .server import build_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="macos-build-test", description="macOS build/test MCP server (HTTP+SSE)."
    )
    sub = parser.add_subparsers(dest="command")
    serve_p = sub.add_parser("serve", help="run the MCP server over SSE")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8970)
    args = parser.parse_args(argv)

    if args.command == "serve":
        build_server(host=args.host, port=args.port).run(transport="sse")
        return 0

    print(f"macos-build-test {__version__}. Commands: serve (see --help).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
