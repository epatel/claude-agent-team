"""WebSocket control surface for the lab (see cards/chat-client.md).

Chat/UI clients connect over WebSocket, submit instructions (enqueued as jobs
into the durable file queue), and receive a live stream of lab events
(job lifecycle + agent output) from the event bus.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets

from .events import EventBus
from .queue import FileQueue

logger = logging.getLogger("dev_lab.server")


async def handle_client_message(
    raw: str, queue: FileQueue, *, default_repo: str | None
) -> dict[str, Any]:
    """Validate and act on one client message; return the reply to send back."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return {"type": "error", "error": "invalid JSON"}
    if not isinstance(msg, dict):
        return {"type": "error", "error": "expected a JSON object"}

    if msg.get("type") == "submit":
        instruction = msg.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            return {"type": "error", "error": "submit requires a non-empty 'instruction'"}
        job = queue.enqueue(instruction, repo=msg.get("repo") or default_repo)
        return {"type": "ack", "job_id": job.id, "instruction": instruction}

    return {"type": "error", "error": f"unknown message type: {msg.get('type')!r}"}


async def _pump(ws, events: asyncio.Queue) -> None:
    try:
        while True:
            await ws.send(json.dumps(await events.get()))
    except websockets.exceptions.ConnectionClosed:
        return


def make_handler(queue: FileQueue, bus: EventBus, *, default_repo: str | None = None):
    async def handler(ws) -> None:
        async with bus.subscribe() as events:
            pump = asyncio.create_task(_pump(ws, events))
            try:
                async for raw in ws:
                    reply = await handle_client_message(raw, queue, default_repo=default_repo)
                    await ws.send(json.dumps(reply))
            except websockets.exceptions.ConnectionClosed:
                pass  # client went away — normal
            finally:
                pump.cancel()

    return handler


async def run_server(
    *,
    host: str,
    port: int,
    queue: FileQueue,
    bus: EventBus,
    default_repo: str | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    handler = make_handler(queue, bus, default_repo=default_repo)
    async with websockets.serve(handler, host, port):
        logger.info("control surface on ws://%s:%d", host, port)
        if stop_event is not None:
            await stop_event.wait()
        else:
            await asyncio.Future()  # run until cancelled
