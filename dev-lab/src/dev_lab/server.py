"""WebSocket control surface for the lab (see cards/chat-client.md).

Two message types from clients:
  - {"type": "submit", "instruction"}  → enqueue a fire-and-forget job (own branch).
  - {"type": "message", "text"}        → an interactive chat turn on this
    connection's session (one branch, resumed agent context across turns).

Lab events (job/turn lifecycle, agent output, tool calls) are published to the
event bus and forwarded to connected clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets

from .config import Config
from .events import EventBus
from .queue import FileQueue
from .session import LabSession

logger = logging.getLogger("dev_lab.server")


async def handle_client_message(
    raw: str, queue: FileQueue, *, default_repo: str | None
) -> dict[str, Any]:
    """Validate and act on a non-interactive message; return the reply to send."""
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


async def _run_interactive_turn(
    bus: EventBus, lock: asyncio.Lock, session: LabSession, text: str
) -> None:
    if not isinstance(text, str) or not text.strip():
        await bus.publish({"type": "error", "error": "message requires non-empty 'text'"})
        return

    async def on_event(event: dict) -> None:
        await bus.publish({**event, "branch": session.branch})

    await bus.publish({"type": "turn_running", "branch": session.branch, "text": text})
    async with lock:  # one agent run at a time over the shared clone
        try:
            result = await session.run_turn(text, on_event=on_event)
        except Exception as exc:  # noqa: BLE001 — surface, keep the connection alive
            await bus.publish({"type": "turn_failed", "branch": session.branch, "error": repr(exc)})
            return
    await bus.publish(
        {
            "type": "turn_done",
            "branch": session.branch,
            "commit_sha": result.commit_sha,
            "committed": result.committed,
        }
    )


def make_handler(
    queue: FileQueue,
    bus: EventBus,
    *,
    config: Config | None = None,
    default_repo: str | None = None,
    lock: asyncio.Lock | None = None,
):
    lock = lock or asyncio.Lock()

    async def handler(ws) -> None:
        session: LabSession | None = None
        async with bus.subscribe() as events:
            pump = asyncio.create_task(_pump(ws, events))
            try:
                async for raw in ws:
                    mtype = None
                    try:
                        parsed = json.loads(raw)
                        mtype = parsed.get("type") if isinstance(parsed, dict) else None
                    except json.JSONDecodeError:
                        parsed = None

                    if mtype == "message" and config is not None and default_repo is not None:
                        if session is None:
                            session = LabSession(repo_path=default_repo, config=config)
                        await _run_interactive_turn(bus, lock, session, parsed.get("text"))
                    else:
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
    config: Config | None = None,
    default_repo: str | None = None,
    lock: asyncio.Lock | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    handler = make_handler(queue, bus, config=config, default_repo=default_repo, lock=lock)
    async with websockets.serve(handler, host, port):
        logger.info("control surface on ws://%s:%d", host, port)
        if stop_event is not None:
            await stop_event.wait()
        else:
            await asyncio.Future()  # run until cancelled
