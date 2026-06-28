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
import fnmatch
import shutil
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


class MCPBridge:
    """One local stdio MCP server, serving requests tunneled from the lab.

    The subprocess (e.g. ``npx @playwright/mcp``) is spawned lazily on the
    first request and lives until the runtime exits — so server-side state
    (a loaded browser page, a session) persists across tool calls. Requests
    are serialized through a queue; a dead server fails fast rather than
    hanging callers. No auto-restart: restart the platform client to recover.
    """

    def __init__(self, name: str, command: str) -> None:
        self.name = name
        self.command = command
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def request(self, method: str, params: dict) -> dict:
        if self._task is None:
            self._task = asyncio.create_task(self._serve())
        if self._task.done():
            exc = self._task.exception()
            raise RuntimeError(f"mcp server {self.name!r} is not running: {exc!r}")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put((method, params, future))
        return await future

    async def _serve(self) -> None:
        import shlex

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        argv = shlex.split(self.command)
        server = StdioServerParameters(command=argv[0], args=argv[1:])
        try:
            async with stdio_client(server) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    while True:
                        method, params, future = await self._queue.get()
                        try:
                            if method == "tools/list":
                                result = await session.list_tools()
                            elif method == "tools/call":
                                result = await session.call_tool(
                                    params["name"], params.get("arguments") or {}
                                )
                            else:
                                raise ValueError(f"unsupported mcp method: {method!r}")
                            payload = result.model_dump(mode="json")
                        except Exception as exc:  # noqa: BLE001 — per-request failure
                            if not future.done():
                                future.set_exception(RuntimeError(str(exc)))
                            continue
                        if not future.done():
                            future.set_result(payload)
        finally:
            # the server died (or never started): fail anything still queued
            while not self._queue.empty():
                _, _, future = self._queue.get_nowait()
                if not future.done():
                    future.set_exception(RuntimeError(f"mcp server {self.name!r} exited"))


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
        mcp_servers: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.platform = platform
        self.capabilities = capabilities
        self.mirrors_root = Path(mirrors_root).expanduser()
        self.token = token
        self.command_timeout = command_timeout
        # name -> launch command for local stdio MCP servers the lab may call
        # through this client (announced in hello, tunneled over the socket).
        self._mcp: dict[str, MCPBridge] = {
            name: MCPBridge(name, cmd) for name, cmd in (mcp_servers or {}).items()
        }
        self.assigned_name: str | None = None
        # The lab's stable id (from hello_ok) — namespaces this lab's mirrors so
        # several labs can share one client machine without colliding on
        # same-named projects. None (an older lab) keeps the flat layout.
        self.lab_id: str | None = None

    async def handle(self, conn: Connection) -> None:
        """Run one connection: hello, then serve tasks until the lab closes it."""
        hello: dict = {
            "type": "hello",
            "name": self.name,
            "platform": self.platform,
            "capabilities": self.capabilities,
            # Tell the lab it may split large synced files into chunk frames;
            # an older lab ignores this and keeps sending whole ``file`` frames.
            "chunked_files": True,
        }
        if self.token:
            hello["token"] = self.token
        if self._mcp:
            hello["mcp_servers"] = sorted(self._mcp)
        await conn.send(hello)
        reply = await conn.receive()
        if reply.get("type") != "hello_ok":
            raise ProtocolError(f"expected hello_ok, got {reply.get('type')!r}")
        self.assigned_name = reply.get("name")
        self.lab_id = str(reply["lab"]) if reply.get("lab") else None
        while True:
            try:
                msg = await conn.receive()
            except ConnectionClosed:
                return
            if msg.get("type") == "task":
                await self._handle_task(conn, msg)
            elif msg.get("type") == "fetch":
                await self._handle_fetch(conn, msg)
            elif msg.get("type") == "mirror":
                await self._handle_mirror(conn, msg)
            elif msg.get("type") == "clean":
                await self._handle_clean(conn, msg)
            elif msg.get("type") == "mcp":
                # MCP calls can be slow (a page load); serve them off-loop so
                # tasks/fetches keep flowing while the browser works.
                asyncio.create_task(self._handle_mcp(conn, msg))

    def _mirror_root(self, project: object) -> Path:
        # Lab id and project name come off the wire — keep each a single path
        # component. Mirrors nest per lab so labs never share a directory.
        root = self.mirrors_root
        if self.lab_id:
            root = root / Path(self.lab_id).name
        return root / Path(str(project or "project")).name

    async def _handle_task(self, conn: Connection, msg: dict) -> None:
        task_id = msg["task_id"]
        source: dict[str, str] = msg.get("manifest", {})
        preserve = [str(p) for p in msg.get("preserve") or []]
        root = self._mirror_root(msg.get("project"))
        root.mkdir(parents=True, exist_ok=True)

        # Sync the mirror to the source manifest: fetch the delta, delete strays
        # (stray deletion also removes the previous run's artifacts — except
        # paths matching ``preserve``, which survive between runs).
        need, delete = manifest.delta(source, manifest.scan(root))
        if preserve:
            delete = [
                p for p in delete
                if not any(fnmatch.fnmatch(p, pattern) for pattern in preserve)
            ]
        if need:
            await conn.send({"type": "need", "task_id": task_id, "paths": need})
            remaining = set(need)
            # Partial files arriving as ``file_chunk`` frames, keyed by path until
            # their ``file_end`` lands (the socket preserves chunk order).
            partial: dict[str, list[bytes]] = {}
            while remaining:
                frame = await conn.receive()
                if frame.get("task_id") != task_id:
                    continue
                ftype = frame.get("type")
                if ftype == "file_chunk":
                    partial.setdefault(frame["path"], []).append(
                        base64.b64decode(frame["data"])
                    )
                elif ftype == "file_end":
                    path = frame["path"]
                    manifest.write_file(root, path, b"".join(partial.pop(path, [])))
                    remaining.discard(path)
                elif ftype == "file":
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

        # Pre-run manifest for the changed-report: without preserve the mirror
        # now equals ``source``; with preserve it also holds kept artifacts, so
        # rescan — otherwise untouched artifacts would show as "added" each run.
        before = manifest.scan(root) if preserve else source
        ok, returncode, stdout, stderr = await asyncio.to_thread(
            _run_command, root, msg["command"], self.command_timeout
        )
        changed = manifest.changed(before, manifest.scan(root))
        await self._send_result(
            conn, task_id, ok=ok, returncode=returncode, stdout=stdout,
            stderr=stderr, changed=changed,
        )

    async def _handle_fetch(self, conn: Connection, msg: dict) -> None:
        """Send mirror files back to the lab (the reverse of manifest sync)."""
        task_id = msg["task_id"]
        root = self._mirror_root(msg.get("project"))
        for path in msg.get("paths", []):
            try:
                data = manifest.read_file(root, path)
            except (OSError, manifest.PathOutsideRoot) as exc:
                await conn.send({"type": "file", "task_id": task_id, "path": path,
                                 "data": None, "error": str(exc)})
                continue
            if len(data) > manifest.MAX_FILE_BYTES:
                await conn.send({"type": "file", "task_id": task_id, "path": path,
                                 "data": None,
                                 "error": f"file too large ({len(data)} bytes)"})
                continue
            for frame in manifest.file_frames(task_id, path, data):
                await conn.send(frame)
        await conn.send({"type": "fetch_done", "task_id": task_id})

    async def _handle_mirror(self, conn: Connection, msg: dict) -> None:
        """Report what this client's mirror of a project holds (no file content)."""
        root = self._mirror_root(msg.get("project"))
        exists = root.is_dir()
        await conn.send(
            {
                "type": "mirror_result",
                "task_id": msg["task_id"],
                "exists": exists,
                "manifest": manifest.scan(root) if exists else {},
            }
        )

    async def _handle_clean(self, conn: Connection, msg: dict) -> None:
        """Delete the project's mirror — the lab-driven "remove my files" action."""
        root = self._mirror_root(msg.get("project"))
        frame: dict = {"type": "clean_done", "task_id": msg["task_id"], "ok": True}
        try:
            if root.is_dir():
                await asyncio.to_thread(shutil.rmtree, root)
        except OSError as exc:
            frame.update(ok=False, error=str(exc))
        await conn.send(frame)

    async def _handle_mcp(self, conn: Connection, msg: dict) -> None:
        """Forward one tunneled MCP request to a local stdio MCP server."""
        frame: dict = {"type": "mcp_result", "task_id": msg["task_id"]}
        bridge = self._mcp.get(str(msg.get("server")))
        if bridge is None:
            frame.update(ok=False, error=f"no mcp server named {msg.get('server')!r}")
        else:
            try:
                result = await bridge.request(
                    str(msg.get("method")), msg.get("params") or {}
                )
                frame.update(ok=True, result=result)
            except Exception as exc:  # noqa: BLE001 — surfaced to the lab/agent
                frame.update(ok=False, error=str(exc))
        await conn.send(frame)

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
        self._send_lock = asyncio.Lock()

    async def send(self, message: dict) -> None:
        import json

        async with self._send_lock:  # mcp results are sent from their own tasks
            await self._ws.send(json.dumps(message))

    async def receive(self) -> dict:
        import json

        import websockets

        try:
            return json.loads(await self._ws.recv())
        except websockets.ConnectionClosed as exc:
            # Surface *why* the socket dropped — without this the connect loop
            # only ever reports a generic "closed", hiding proxy frame-size
            # kills (1009), idle/abnormal closes (1006), server errors (1011),
            # etc. Code 1006 with no reason almost always means a reverse proxy
            # or load balancer cut the connection (frame too big / idle timeout).
            close = exc.rcvd or exc.sent
            code = close.code if close else "?"
            reason = (close.reason if close else "") or "(no reason)"
            print(f"[platform-client] socket closed: code={code} reason={reason}", flush=True)
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
