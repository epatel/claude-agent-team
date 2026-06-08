"""Chat/UI control client for the dev lab — talks to the lab over WebSocket.

  chat-client submit "<instruction>"   # send one instruction, stream until it finishes
  chat-client listen                    # stream all lab events
"""

from __future__ import annotations

import argparse
import asyncio
import json

import websockets

from . import __version__
from .client import format_event

DEFAULT_URL = "ws://127.0.0.1:8765"


async def _listen(url: str) -> None:
    async with websockets.connect(url) as ws:
        async for raw in ws:
            print(format_event(json.loads(raw)), flush=True)


async def _submit(url: str, instruction: str) -> None:
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "submit", "instruction": instruction}))
        job_id = None
        async for raw in ws:
            event = json.loads(raw)
            print(format_event(event), flush=True)
            if event.get("type") == "ack":
                job_id = event.get("job_id")
            elif event.get("type") in ("job_done", "job_failed") and event.get("job_id") == job_id:
                break


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="chat-client", description="Chat/UI control surface for the dev lab."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="lab control WebSocket URL")
    sub = parser.add_subparsers(dest="command")
    submit_p = sub.add_parser("submit", help="send one instruction and stream until it finishes")
    submit_p.add_argument("instruction")
    sub.add_parser("listen", help="stream all lab events")
    args = parser.parse_args(argv)

    if args.command == "submit":
        asyncio.run(_submit(args.url, args.instruction))
        return 0
    if args.command == "listen":
        try:
            asyncio.run(_listen(args.url))
        except KeyboardInterrupt:
            pass
        return 0

    print(f"chat-client {__version__}. Commands: submit | listen (see --help).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
