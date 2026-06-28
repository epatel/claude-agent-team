"""Registry and dispatch for connected platform clients (cards/extension-clients.md).

Platform clients dial in over WebSocket (``/ws/client`` in web.py), announce
``{name, platform, capabilities}``, and stay connected — presence here *is* the
registry. The lab dispatches commands to them and serves the manifest-sync file
requests that move the project's working tree (uncommitted state included) into
the client's mirror.

Wire protocol (JSON text frames; the lab owns this contract; ``task_id`` is a
correlation id — fetches use it too):

  client → lab   {type: "hello", name, platform, capabilities, token?, chunked_files?}
  lab → client   {type: "hello_ok", name, lab}       # final (deduped) name + the
                                                     # lab's stable id, which the
                                                     # client uses to namespace
                                                     # its mirrors per lab
  lab → client   {type: "task", task_id, project, command, manifest, preserve?}
  client → lab   {type: "need", task_id, paths}      # stale/missing in mirror
  lab → client   {type: "file", task_id, path, data} # base64; data=None + error on read failure
  client → lab   {type: "result", task_id, ok, returncode, stdout, stderr, changed}
  lab → client   {type: "fetch", task_id, project, paths}   # pull mirror files back
  client → lab   {type: "file", task_id, path, data}        # same frame, reverse direction
  client → lab   {type: "fetch_done", task_id}

  Large files (> manifest.FILE_CHUNK_BYTES) are sent split, in either direction,
  so no single WebSocket frame trips a reverse proxy's message-size limit:
      {type: "file_chunk", task_id, path, seq, data}   # ordered base64 pieces
      {type: "file_end", task_id, path}                # concatenate, then write
  The lab only chunks toward a client whose hello set ``chunked_files: true``; an
  older client keeps receiving whole ``file`` frames. (manifest.file_frames is
  the single source for this split; both ends reassemble in arrival order.)
  lab → client   {type: "mirror", task_id, project}         # inspect the mirror
  client → lab   {type: "mirror_result", task_id, exists, manifest}
  lab → client   {type: "clean", task_id, project}          # delete the mirror
  client → lab   {type: "clean_done", task_id, ok, error?}
  lab → client   {type: "mcp", task_id, server, method, params}   # tunneled MCP
  client → lab   {type: "mcp_result", task_id, ok, result|error}  # (tools/list,
                                                     # tools/call against a stdio
                                                     # MCP server the client runs
                                                     # locally, e.g. a browser)
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
class _Fetch:
    future: asyncio.Future
    files: dict[str, bytes] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    # Chunk frames accumulate here per path until their ``file_end`` arrives.
    partial: dict[str, list[bytes]] = field(default_factory=dict)


@dataclass
class _Client:
    name: str
    platform: str
    capabilities: list[dict]
    # Names of local stdio MCP servers announced in the hello — callable
    # through the tunnel (mcp/mcp_result frames).
    mcp_servers: list[str]
    send: Send
    # The client understands chunk frames (announced ``chunked_files`` in its
    # hello); gates whether the lab splits large synced files toward it.
    chunked_files: bool = False
    tasks: dict[str, _Task] = field(default_factory=dict)
    fetches: dict[str, _Fetch] = field(default_factory=dict)
    # Single-frame request/response exchanges (mirror_result, clean_done).
    requests: dict[str, asyncio.Future] = field(default_factory=dict)


class ClientRegistry:
    """Connected platform clients and the request/response plumbing to them."""

    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self._clients: dict[str, _Client] = {}
        # Called whenever a client's in-flight task count changes, so the
        # console can refresh the per-client "busy" badge (web.py wires it to a
        # ``clients_changed`` publish). register/unregister publish on their own.
        self._on_change = on_change

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change()

    @staticmethod
    def _busy(c: _Client) -> int:
        """In-flight work dispatched to a client: runs + fetches + requests."""
        return len(c.tasks) + len(c.fetches) + len(c.requests)

    # -- connection lifecycle ------------------------------------------------

    def register(
        self,
        *,
        name: str,
        platform: str,
        capabilities: list[dict],
        send: Send,
        mcp_servers: list[str] | None = None,
        chunked_files: bool = False,
    ) -> str:
        """Add a connected client; a taken name gets a ``_2`` / ``_3`` … suffix."""
        final = name or "client"
        n = 2
        while final in self._clients:
            final = f"{name}_{n}"
            n += 1
        self._clients[final] = _Client(
            final, platform, capabilities, list(mcp_servers or []), send,
            chunked_files=chunked_files,
        )
        return final

    def unregister(self, name: str) -> None:
        """Drop a client; any in-flight task or fetch fails rather than hangs."""
        client = self._clients.pop(name, None)
        if client is None:
            return
        pending = [t.future for t in client.tasks.values()]
        pending += [f.future for f in client.fetches.values()]
        pending += list(client.requests.values())
        for future in pending:
            if not future.done():
                future.set_exception(ClientError(f"client {name} disconnected"))

    def list(self) -> list[dict]:
        return [
            {
                "name": c.name, "platform": c.platform,
                "capabilities": c.capabilities, "mcp_servers": c.mcp_servers,
                "busy": self._busy(c),
            }
            for c in sorted(self._clients.values(), key=lambda c: c.name)
        ]

    def get(self, name: str) -> dict | None:
        c = self._clients.get(name)
        return None if c is None else {
            "name": c.name, "platform": c.platform,
            "capabilities": c.capabilities, "mcp_servers": c.mcp_servers,
            "busy": self._busy(c),
        }

    # -- task dispatch ---------------------------------------------------------

    async def run(
        self,
        name: str,
        *,
        project_root: Path,
        command: str,
        preserve: list[str] | None = None,
        timeout: float = 900.0,
    ) -> dict:
        """Sync ``project_root`` to ``name``'s mirror and run ``command`` there.

        ``preserve`` is a list of glob patterns for mirror paths the sync must
        NOT delete even though they aren't in the source tree — build artifacts
        and caches that should survive between runs. Returns the client's
        result dict (ok/returncode/stdout/stderr/changed, plus the manifest
        hash that identifies exactly what ran). Raises ``ClientError`` if the
        client is unknown, disconnects mid-task, or the task times out.
        """
        client = self._clients.get(name)
        if client is None:
            raise ClientError(f"no connected client named {name!r}")
        source = manifest.scan(project_root)
        task_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        client.tasks[task_id] = _Task(root=project_root, future=future)
        self._notify()
        try:
            message = {
                "type": "task",
                "task_id": task_id,
                "project": project_root.name,
                "command": command,
                "manifest": source,
            }
            if preserve:
                message["preserve"] = list(preserve)
            await client.send(message)
            try:
                result = await asyncio.wait_for(future, timeout)
            except TimeoutError:
                raise ClientError(f"task on {name} timed out after {timeout}s") from None
            return {**result, "manifest_hash": manifest.manifest_hash(source)}
        finally:
            client.tasks.pop(task_id, None)
            self._notify()

    async def fetch(
        self, name: str, *, project: str, paths: list[str], timeout: float = 120.0
    ) -> dict:
        """Pull files from ``name``'s mirror of ``project`` back to the lab.

        The reverse of manifest sync — how run artifacts (binaries, reports)
        reach the lab. Returns ``{"files": {path: bytes}, "errors": {path:
        reason}}``; a missing or oversized file is an error entry, not an
        exception. Raises ``ClientError`` on unknown client / disconnect /
        timeout.
        """
        client = self._clients.get(name)
        if client is None:
            raise ClientError(f"no connected client named {name!r}")
        fetch_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        client.fetches[fetch_id] = _Fetch(future=future)
        self._notify()
        try:
            await client.send(
                {"type": "fetch", "task_id": fetch_id, "project": project,
                 "paths": list(paths)}
            )
            try:
                return await asyncio.wait_for(future, timeout)
            except TimeoutError:
                raise ClientError(f"fetch from {name} timed out after {timeout}s") from None
        finally:
            client.fetches.pop(fetch_id, None)
            self._notify()

    async def mirror(self, name: str, *, project: str, timeout: float = 60.0) -> dict:
        """Ask ``name`` what its mirror of ``project`` holds.

        Returns ``{"exists": bool, "manifest": {path: hash}}`` — the same
        manifest shape the sync uses, so the web console can browse a client's
        mirror (run artifacts included) without pulling any file content.
        Raises ``ClientError`` on unknown client / disconnect / timeout.
        """
        return await self._request(
            name, {"type": "mirror", "project": project}, timeout=timeout,
            describe=f"mirror listing from {name}",
        )

    async def clean(self, name: str, *, project: str, timeout: float = 60.0) -> dict:
        """Delete ``name``'s mirror of ``project`` (files on the client machine).

        Returns ``{"ok": bool, "error": str | None}``. Raises ``ClientError``
        on unknown client / disconnect / timeout.
        """
        return await self._request(
            name, {"type": "clean", "project": project}, timeout=timeout,
            describe=f"mirror clean on {name}",
        )

    async def mcp_call(
        self, name: str, *, server: str, method: str, params: dict | None = None,
        timeout: float = 120.0,
    ) -> dict:
        """Tunnel one MCP request (tools/list, tools/call) to ``name``'s server.

        The client runs the MCP server locally (announced in its hello) and
        owns its state — e.g. a browser page stays loaded between calls.
        Returns the raw MCP result dict; raises ``ClientError`` on a tunnel or
        server-side failure so agent tools surface a readable error.
        """
        client = self._clients.get(name)
        if client is None:
            raise ClientError(f"no connected client named {name!r}")
        if server not in client.mcp_servers:
            raise ClientError(
                f"client {name!r} announces no mcp server named {server!r} "
                f"(has: {', '.join(client.mcp_servers) or 'none'})"
            )
        reply = await self._request(
            name,
            {"type": "mcp", "server": server, "method": method, "params": params or {}},
            timeout=timeout,
            describe=f"mcp {method} on {name}/{server}",
        )
        if not reply.get("ok"):
            raise ClientError(str(reply.get("error") or "mcp call failed"))
        return reply.get("result") or {}

    async def _request(
        self, name: str, message: dict, *, timeout: float, describe: str
    ) -> dict:
        """Send one frame and await its single-frame reply (same task_id)."""
        client = self._clients.get(name)
        if client is None:
            raise ClientError(f"no connected client named {name!r}")
        request_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        client.requests[request_id] = future
        self._notify()
        try:
            await client.send({**message, "task_id": request_id})
            try:
                return await asyncio.wait_for(future, timeout)
            except TimeoutError:
                raise ClientError(f"{describe} timed out after {timeout}s") from None
        finally:
            client.requests.pop(request_id, None)
            self._notify()

    # -- incoming messages -----------------------------------------------------

    async def handle_message(self, name: str, msg: dict) -> None:
        """Route a post-hello message from a connected client."""
        client = self._clients.get(name)
        if client is None:
            return
        correlation = msg.get("task_id", "")
        task = client.tasks.get(correlation)
        fetch = client.fetches.get(correlation)
        request = client.requests.get(correlation)
        if request is not None and not request.done():
            if msg.get("type") == "mirror_result":
                request.set_result(
                    {"exists": bool(msg.get("exists")), "manifest": msg.get("manifest") or {}}
                )
            elif msg.get("type") == "clean_done":
                request.set_result(
                    {"ok": bool(msg.get("ok")), "error": msg.get("error")}
                )
            elif msg.get("type") == "mcp_result":
                request.set_result(
                    {
                        "ok": bool(msg.get("ok")),
                        "result": msg.get("result"),
                        "error": msg.get("error"),
                    }
                )
        elif task is not None:
            if msg.get("type") == "need":
                for path in msg.get("paths", []):
                    await self._send_file(client, correlation, task.root, path)
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
        elif fetch is not None:
            if msg.get("type") == "file":
                if msg.get("data") is None:
                    fetch.errors[msg.get("path", "?")] = str(msg.get("error") or "unreadable")
                else:
                    fetch.files[msg["path"]] = base64.b64decode(msg["data"])
            elif msg.get("type") == "file_chunk":
                fetch.partial.setdefault(msg["path"], []).append(
                    base64.b64decode(msg["data"])
                )
            elif msg.get("type") == "file_end":
                path = msg["path"]
                fetch.files[path] = b"".join(fetch.partial.pop(path, []))
            elif msg.get("type") == "fetch_done" and not fetch.future.done():
                fetch.future.set_result({"files": fetch.files, "errors": fetch.errors})
        # unknown correlation id: finished/timed out — stale traffic is dropped

    @staticmethod
    async def _send_file(client: _Client, task_id: str, root: Path, path: str) -> None:
        """Send one mirror file to ``client``, split into chunks when it's large
        and the client negotiated chunking; a read error becomes a single
        ``file`` frame with ``data: None`` so the sync fails cleanly."""
        try:
            data = manifest.read_file(root, path)
        except (OSError, manifest.PathOutsideRoot) as exc:
            await client.send({"type": "file", "task_id": task_id, "path": path,
                               "data": None, "error": str(exc)})
            return
        for frame in manifest.file_frames(
            task_id, path, data, chunk=client.chunked_files
        ):
            await client.send(frame)
