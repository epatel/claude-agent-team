"""Registry and dispatch for connected platform clients (cards/extension-clients.md).

Platform clients dial in over WebSocket (``/ws/client`` in web.py), announce
``{name, platform, capabilities}``, and stay connected — presence here *is* the
registry. The lab dispatches commands to them and serves the manifest-sync file
requests that move the project's working tree (uncommitted state included) into
the client's mirror.

Wire protocol (JSON text frames; the lab owns this contract):

  client → lab   {type: "hello", name, platform, capabilities, token?}
  lab → client   {type: "hello_ok", name}            # final (deduped) name
  lab → client   {type: "task", task_id, project, command, manifest}
  client → lab   {type: "need", task_id, paths}      # stale/missing in mirror
  lab → client   {type: "file", task_id, path, data} # base64; data=None + error on read failure
  client → lab   {type: "result", task_id, ok, returncode, stdout, stderr, changed}
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from platform_client import manifest

Send = Callable[[dict], Awaitable[None]]


class ClientError(RuntimeError):
    pass


@dataclass
class _Task:
    root: Path
    future: asyncio.Future


@dataclass
class _Client:
    name: str
    platform: str
    capabilities: list[dict]
    send: Send
    tasks: dict[str, _Task] = field(default_factory=dict)


class ClientRegistry:
    """Connected platform clients and the request/response plumbing to them."""

    def __init__(self) -> None:
        self._clients: dict[str, _Client] = {}

    # -- connection lifecycle ------------------------------------------------

    def register(
        self, *, name: str, platform: str, capabilities: list[dict], send: Send
    ) -> str:
        """Add a connected client; a taken name gets a ``_2`` / ``_3`` … suffix."""
        final = name or "client"
        n = 2
        while final in self._clients:
            final = f"{name}_{n}"
            n += 1
        self._clients[final] = _Client(final, platform, capabilities, send)
        return final

    def unregister(self, name: str) -> None:
        """Drop a client; any in-flight task fails rather than hangs."""
        client = self._clients.pop(name, None)
        if client is None:
            return
        for task in client.tasks.values():
            if not task.future.done():
                task.future.set_exception(ClientError(f"client {name} disconnected"))

    def list(self) -> list[dict]:
        return [
            {"name": c.name, "platform": c.platform, "capabilities": c.capabilities}
            for c in sorted(self._clients.values(), key=lambda c: c.name)
        ]

    def get(self, name: str) -> dict | None:
        c = self._clients.get(name)
        return None if c is None else {
            "name": c.name, "platform": c.platform, "capabilities": c.capabilities
        }

    # -- task dispatch ---------------------------------------------------------

    async def run(
        self, name: str, *, project_root: Path, command: str, timeout: float = 900.0
    ) -> dict:
        """Sync ``project_root`` to ``name``'s mirror and run ``command`` there.

        Returns the client's result dict (ok/returncode/stdout/stderr/changed,
        plus the manifest hash that identifies exactly what ran). Raises
        ``ClientError`` if the client is unknown, disconnects mid-task, or the
        task times out.
        """
        client = self._clients.get(name)
        if client is None:
            raise ClientError(f"no connected client named {name!r}")
        source = manifest.scan(project_root)
        task_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        client.tasks[task_id] = _Task(root=project_root, future=future)
        try:
            await client.send(
                {
                    "type": "task",
                    "task_id": task_id,
                    "project": project_root.name,
                    "command": command,
                    "manifest": source,
                }
            )
            try:
                result = await asyncio.wait_for(future, timeout)
            except TimeoutError:
                raise ClientError(f"task on {name} timed out after {timeout}s") from None
            return {**result, "manifest_hash": manifest.manifest_hash(source)}
        finally:
            client.tasks.pop(task_id, None)

    # -- incoming messages -----------------------------------------------------

    async def handle_message(self, name: str, msg: dict) -> None:
        """Route a post-hello message from a connected client."""
        client = self._clients.get(name)
        if client is None:
            return
        task = client.tasks.get(msg.get("task_id", ""))
        if task is None:
            return  # task finished/timed out — stale traffic is dropped
        if msg.get("type") == "need":
            for path in msg.get("paths", []):
                await client.send(self._file_message(task.root, msg["task_id"], path))
        elif msg.get("type") == "result" and not task.future.done():
            task.future.set_result(
                {
                    "ok": bool(msg.get("ok")),
                    "returncode": msg.get("returncode"),
                    "stdout": msg.get("stdout", ""),
                    "stderr": msg.get("stderr", ""),
                    "changed": msg.get("changed", {}),
                }
            )

    @staticmethod
    def _file_message(root: Path, task_id: str, path: str) -> dict:
        try:
            data = base64.b64encode(manifest.read_file(root, path)).decode()
        except (OSError, manifest.PathOutsideRoot) as exc:
            return {"type": "file", "task_id": task_id, "path": path, "data": None,
                    "error": str(exc)}
        return {"type": "file", "task_id": task_id, "path": path, "data": data}
