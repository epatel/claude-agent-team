"""The platform-client runtime: dial the lab, announce, sync, run, report.

The runtime side of the wire protocol owned by the lab (dev_lab/clients.py has
the message reference). ``ClientRuntime`` is transport-agnostic — it talks to a
``Connection`` (async ``send``/``receive`` of dicts) so tests drive it with a
fake; ``connect_forever`` wraps it in a real websocket with reconnect.

Each task syncs the lab's manifest into a per-project mirror (fetch the delta,
delete strays — which also clears the previous run's artifacts), runs the
command there, and reports the result plus what the run changed.
"""

from __future__ import annotations

import asyncio
import base64
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from . import manifest

_MAX_OUTPUT = 20_000  # chars per stream, to keep results bounded
_WS_MAX_MESSAGE = 32 * 1024 * 1024  # fits the largest synced file, base64-encoded


class ConnectionClosed(Exception):
    """The lab went away; ``handle`` returns and the connect loop retries."""


class ProtocolError(RuntimeError):
    pass


class Connection(Protocol):
    async def send(self, message: dict) -> None: ...
    async def receive(self) -> dict: ...


def _truncate(text: str) -> str:
    return text if len(text) <= _MAX_OUTPUT else text[:_MAX_OUTPUT] + "\n...[truncated]"


def _run_command(root: Path, command: str, timeout: float) -> tuple[bool, int, str, str]:
    try:
        proc = subprocess.run(
            command, shell=True, cwd=root, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return False, 124, "", f"timed out after {timeout}s"
    return proc.returncode == 0, proc.returncode, _truncate(proc.stdout), _truncate(proc.stderr)


class ClientRuntime:
    """One platform client: identity, capabilities, and the mirror directory."""

    def __init__(
        self,
        *,
        name: str,
        capabilities: list[dict],
        mirrors_root: str | Path,
        platform: str = sys.platform,
        token: str | None = None,
        command_timeout: float = 900.0,
    ) -> None:
        self.name = name
        self.platform = platform
        self.capabilities = capabilities
        self.mirrors_root = Path(mirrors_root).expanduser()
        self.token = token
        self.command_timeout = command_timeout
        self.assigned_name: str | None = None

    async def handle(self, conn: Connection) -> None:
        """Run one connection: hello, then serve tasks until the lab closes it."""
        hello: dict = {
            "type": "hello",
            "name": self.name,
            "platform": self.platform,
            "capabilities": self.capabilities,
        }
        if self.token:
            hello["token"] = self.token
        await conn.send(hello)
        reply = await conn.receive()
        if reply.get("type") != "hello_ok":
            raise ProtocolError(f"expected hello_ok, got {reply.get('type')!r}")
        self.assigned_name = reply.get("name")
        while True:
            try:
                msg = await conn.receive()
            except ConnectionClosed:
                return
            if msg.get("type") == "task":
                await self._handle_task(conn, msg)

    async def _handle_task(self, conn: Connection, msg: dict) -> None:
        task_id = msg["task_id"]
        source: dict[str, str] = msg.get("manifest", {})
        # The project name comes off the wire — keep it a single path component.
        root = self.mirrors_root / Path(str(msg.get("project") or "project")).name
        root.mkdir(parents=True, exist_ok=True)

        # Sync the mirror to the source manifest: fetch the delta, delete strays
        # (stray deletion also removes the previous run's artifacts).
        need, delete = manifest.delta(source, manifest.scan(root))
        if need:
            await conn.send({"type": "need", "task_id": task_id, "paths": need})
            remaining = set(need)
            while remaining:
                frame = await conn.receive()
                if frame.get("type") != "file" or frame.get("task_id") != task_id:
                    continue
                if frame.get("data") is None:
                    await self._send_result(
                        conn, task_id, ok=False, returncode=1, stdout="",
                        stderr=f"sync failed for {frame.get('path')}: {frame.get('error')}",
                        changed={},
                    )
                    return
                manifest.write_file(root, frame["path"], base64.b64decode(frame["data"]))
                remaining.discard(frame["path"])
        manifest.delete_files(root, delete)

        # The mirror now equals ``source``, so it doubles as the pre-run manifest.
        ok, returncode, stdout, stderr = await asyncio.to_thread(
            _run_command, root, msg["command"], self.command_timeout
        )
        changed = manifest.changed(source, manifest.scan(root))
        await self._send_result(
            conn, task_id, ok=ok, returncode=returncode, stdout=stdout,
            stderr=stderr, changed=changed,
        )

    @staticmethod
    async def _send_result(
        conn: Connection, task_id: str, *, ok: bool, returncode: int,
        stdout: str, stderr: str, changed: dict,
    ) -> None:
        await conn.send(
            {
                "type": "result",
                "task_id": task_id,
                "ok": ok,
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
                "changed": changed,
            }
        )


class _WsConnection:
    """Adapt a ``websockets`` client connection to the ``Connection`` protocol."""

    def __init__(self, ws) -> None:
        self._ws = ws

    async def send(self, message: dict) -> None:
        import json

        await self._ws.send(json.dumps(message))

    async def receive(self) -> dict:
        import json

        import websockets

        try:
            return json.loads(await self._ws.recv())
        except websockets.ConnectionClosed as exc:
            raise ConnectionClosed from exc


async def connect_once(url: str, runtime: ClientRuntime) -> None:
    """One connection to the lab's ``/ws/client``; returns when it closes."""
    import websockets

    async with websockets.connect(url, max_size=_WS_MAX_MESSAGE) as ws:
        await runtime.handle(_WsConnection(ws))


async def connect_forever(url: str, runtime: ClientRuntime, *, retry_delay: float = 5.0) -> None:
    """Keep the client connected: reconnect (and re-announce) after any drop."""
    while True:
        try:
            await connect_once(url, runtime)
            print(f"[platform-client] connection to {url} closed; reconnecting")
        except ProtocolError:
            raise  # wrong endpoint or rejected hello — retrying won't help
        except Exception as exc:  # noqa: BLE001 — network errors: log and retry
            print(f"[platform-client] {exc!r}; retrying in {retry_delay}s")
        await asyncio.sleep(retry_delay)
