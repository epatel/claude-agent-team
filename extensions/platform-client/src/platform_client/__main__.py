"""Entry point: run this machine as a platform client of the lab.

  platform-client connect --lab ws://pi.local:8000/ws/client \
      --name mac --capability run_tests --capability build

The token comes from --token or the CLIENT_TOKEN env var (must match the lab's
when the lab has one configured). Mirrors live under --mirrors (default
~/.platform-client/mirrors), one subdirectory per project, kept warm and in
sync between runs.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket

from .runtime import ClientRuntime, connect_forever, connect_once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="platform-client",
        description="Connect this machine to the lab as a capability provider.",
    )
    sub = parser.add_subparsers(dest="command")
    c = sub.add_parser("connect", help="dial the lab and serve tasks")
    c.add_argument("--lab", required=True, help="lab client endpoint, e.g. ws://pi:8000/ws/client")
    c.add_argument("--name", default=socket.gethostname().split(".")[0])
    c.add_argument("--token", default=os.environ.get("CLIENT_TOKEN"))
    c.add_argument("--mirrors", default="~/.platform-client/mirrors")
    c.add_argument(
        "--capability", action="append", dest="capabilities", metavar="NAME",
        help="a capability to announce (repeatable); default: run",
    )
    c.add_argument(
        "--mcp", action="append", dest="mcp", metavar="NAME=COMMAND",
        help="a local stdio MCP server the lab's agents may call through this "
             "client (repeatable), e.g. --mcp browser=\"npx @playwright/mcp@latest\"",
    )
    c.add_argument("--once", action="store_true", help="exit when the connection closes")
    args = parser.parse_args(argv)

    if args.command == "connect":
        mcp_servers: dict[str, str] = {}
        for entry in args.mcp or []:
            name, sep, command = entry.partition("=")
            if not sep or not name.strip() or not command.strip():
                parser.error(f"bad --mcp entry (want NAME=COMMAND): {entry!r}")
            mcp_servers[name.strip()] = command.strip()
        runtime = ClientRuntime(
            name=args.name,
            capabilities=[{"name": n} for n in (args.capabilities or ["run"])],
            mirrors_root=args.mirrors,
            token=args.token,
            mcp_servers=mcp_servers,
        )
        run = connect_once(args.lab, runtime) if args.once else connect_forever(args.lab, runtime)
        try:
            asyncio.run(run)
        except KeyboardInterrupt:
            pass
        return 0

    print("platform-client. Commands: connect (see --help).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
