"""FastAPI web console: login, project list, and per-project chat over WebSocket.

The primary control surface for v2. Each project (a clone under ``labs/``) is its
own Claude agent/context; the WS streams turn lifecycle, agent text, and tool
calls, and persists the conversation per project.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, Response
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from . import auth, db
from .clients import ClientError, ClientRegistry
from .config import KNOWN_MODELS, Config
from .events import EventBus
from .projects import ProjectError, ProjectManager
from .workspace import WorkspaceError


def _display_blob(path: str, data: bytes, *, max_bytes: int = 512 * 1024) -> dict:
    """Shape one file's bytes for the browser, like ``Workspace.read_file``.

    Used for files that live on a platform client (fetched over the socket),
    where there is no local path to read — same binary/oversize flags so the
    file viewer renders both sources identically.
    """
    size = len(data)
    chunk = data[:max_bytes]
    if b"\x00" in chunk:
        return {"path": path, "binary": True, "size": size, "truncated": False, "content": ""}
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return {"path": path, "binary": True, "size": size, "truncated": False, "content": ""}
    return {
        "path": path,
        "binary": False,
        "size": size,
        "truncated": size > max_bytes,
        "content": text,
    }


def _project_dict(pm: ProjectManager, row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "branch": row["branch"],
        "base_branch": pm.effective_base(row),
        # Whether a GitHub token is configured — never the token itself.
        "has_token": bool(row["github_token"]),
        # The model this project will run with (its override, else the lab default).
        "model": pm.effective_model(row),
    }


async def _run_turn(pm: ProjectManager, bus: EventBus, project_id: int, text: str) -> None:
    async def on_event(event: dict) -> None:
        await bus.publish({**event, "project_id": project_id})

    await bus.publish({"type": "turn_running", "project_id": project_id, "text": text})
    try:
        result = await pm.run_turn(project_id, text, on_event=on_event)
    except Exception as exc:  # noqa: BLE001 — surface, keep the socket alive
        await bus.publish({"type": "turn_failed", "project_id": project_id, "error": repr(exc)})
        return
    await bus.publish(
        {
            "type": "turn_done",
            "project_id": project_id,
            "branch": result.branch,
            "commit_sha": result.commit_sha,
            "committed": result.committed,
        }
    )


async def _ws_pump(ws: WebSocket, events: asyncio.Queue) -> None:
    try:
        while True:
            await ws.send_text(json.dumps(await events.get()))
    except (WebSocketDisconnect, RuntimeError):
        return


def build_app(
    *,
    labs_dir: str | Path,
    config: Config,
    conn: sqlite3.Connection,
    secret: str,
    bus: EventBus | None = None,
    static_dir: str | Path | None = None,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="lax")
    bus = bus or EventBus()
    registry = ClientRegistry()
    app.state.registry = registry  # reachable from tests
    pm = ProjectManager(labs_dir=labs_dir, config=config, conn=conn, client_registry=registry)
    pm.discover()

    def current_user(request: Request) -> sqlite3.Row:
        uid = request.session.get("user_id")
        user = auth.get_user(conn, uid) if uid else None
        if user is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        if user["blocked"]:
            request.session.clear()
            raise HTTPException(status_code=403, detail="account blocked")
        return user

    def require_super(request: Request) -> sqlite3.Row:
        user = current_user(request)
        if not user["is_super"]:
            raise HTTPException(status_code=403, detail="super-user only")
        return user

    @app.post("/api/register")
    async def register(request: Request) -> dict:
        data = await request.json()
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        invite = (data.get("invite") or "").strip()
        if not username or not password:
            raise HTTPException(400, "username and password required")
        # The first ever user is the super-user and needs no invite. Everyone
        # after must redeem a valid, unused invite code.
        first_user = auth.count_users(conn) == 0
        if not first_user:
            row = auth.get_invite(conn, invite)
            if row is None or row["used_by"] is not None:
                raise HTTPException(403, "a valid invite code is required to register")
        try:
            uid = auth.create_user(conn, username, password, is_super=first_user)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not first_user and not auth.consume_invite(conn, invite, uid):
            # Lost a race for the same code — undo the half-made account.
            auth.delete_user(conn, uid)
            raise HTTPException(403, "a valid invite code is required to register")
        request.session["user_id"] = uid
        return {"username": username, "is_super": bool(first_user)}

    @app.get("/api/auth/state")
    async def auth_state(request: Request) -> dict:
        # Public: lets the login screen decide whether to show the invite field.
        # The first ever user is the super-user and registers without an invite.
        return {"needs_invite": auth.count_users(conn) > 0}

    @app.post("/api/login")
    async def login(request: Request) -> dict:
        data = await request.json()
        user = auth.verify_user(conn, data.get("username", ""), data.get("password", ""))
        if user is None:
            raise HTTPException(401, "invalid credentials")
        if user["blocked"]:
            raise HTTPException(403, "account blocked")
        request.session["user_id"] = user["id"]
        return {"username": user["username"], "is_super": bool(user["is_super"])}

    @app.post("/api/logout")
    async def logout(request: Request) -> dict:
        request.session.clear()
        return {"ok": True}

    @app.get("/api/me")
    async def me(request: Request) -> dict:
        user = current_user(request)
        return {"username": user["username"], "is_super": bool(user["is_super"])}

    # --- Admin: user db + invites (super-user only) -----------------------

    def _user_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "username": row["username"],
            "is_super": bool(row["is_super"]),
            "blocked": bool(row["blocked"]),
            "created_at": row["created_at"],
        }

    @app.get("/api/admin/users")
    async def admin_list_users(request: Request) -> list[dict]:
        require_super(request)
        return [_user_dict(u) for u in auth.list_users(conn)]

    @app.post("/api/admin/users/{user_id}/block")
    async def admin_block_user(user_id: int, request: Request) -> dict:
        me_row = require_super(request)
        data = await request.json()
        blocked = bool(data.get("blocked", True))
        target = auth.get_user(conn, user_id)
        if target is None:
            raise HTTPException(404, "no such user")
        if user_id == me_row["id"]:
            raise HTTPException(400, "you cannot block yourself")
        auth.set_blocked(conn, user_id, blocked)
        return {"id": user_id, "blocked": blocked}

    @app.post("/api/admin/users/{user_id}/password")
    async def admin_reset_password(user_id: int, request: Request) -> dict:
        require_super(request)
        data = await request.json()
        password = data.get("password") or ""
        if not password:
            raise HTTPException(400, "password required")
        if auth.get_user(conn, user_id) is None:
            raise HTTPException(404, "no such user")
        auth.set_password(conn, user_id, password)
        return {"id": user_id, "ok": True}

    @app.delete("/api/admin/users/{user_id}")
    async def admin_delete_user(user_id: int, request: Request) -> dict:
        me_row = require_super(request)
        if auth.get_user(conn, user_id) is None:
            raise HTTPException(404, "no such user")
        if user_id == me_row["id"]:
            raise HTTPException(400, "you cannot delete yourself")
        auth.delete_user(conn, user_id)
        return {"id": user_id, "deleted": True}

    @app.get("/api/admin/invites")
    async def admin_list_invites(request: Request) -> list[dict]:
        require_super(request)
        return [
            {
                "code": i["code"],
                "created_at": i["created_at"],
                "used_by": i["used_by"],
                "used_at": i["used_at"],
            }
            for i in auth.list_invites(conn)
        ]

    @app.post("/api/admin/invites")
    async def admin_create_invite(request: Request) -> dict:
        me_row = require_super(request)
        code = auth.create_invite(conn, me_row["id"])
        return {"code": code}

    @app.get("/api/models")
    async def list_models(request: Request) -> dict:
        current_user(request)
        # The selectable models plus the lab default (used when a project has no
        # override) so the console can pre-select it.
        return {"models": KNOWN_MODELS, "default": config.model}

    @app.get("/api/clients")
    async def list_clients(request: Request) -> list[dict]:
        current_user(request)
        return registry.list()

    @app.get("/api/projects")
    async def list_projects(request: Request) -> list[dict]:
        current_user(request)
        return [_project_dict(pm, r) for r in pm.discover()]

    @app.post("/api/projects")
    async def create_project(request: Request) -> dict:
        current_user(request)
        data = await request.json()
        remote_url = (data.get("remote_url") or "").strip()
        github_token = (data.get("github_token") or "").strip()
        model = (data.get("model") or "").strip()
        try:
            row = pm.create(remote_url, github_token=github_token, model=model)
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return _project_dict(pm, row)

    @app.post("/api/projects/{project_id}/token")
    async def set_token(project_id: int, request: Request) -> dict:
        current_user(request)
        data = await request.json()
        token = (data.get("github_token") or "").strip()
        try:
            result = await pm.set_token(project_id, token)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

    @app.post("/api/projects/{project_id}/model")
    async def set_model(project_id: int, request: Request) -> dict:
        current_user(request)
        data = await request.json()
        model = (data.get("model") or "").strip()
        try:
            result = await pm.set_model(project_id, model)
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

    @app.post("/api/projects/{project_id}/merge")
    async def merge_project(project_id: int, request: Request) -> dict:
        current_user(request)
        try:
            result = await pm.merge_to_base(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

    @app.post("/api/projects/{project_id}/rebase")
    async def rebase_project(project_id: int, request: Request) -> dict:
        """Rebase the chat branch onto base; conflicts come back as data."""
        current_user(request)
        try:
            result = await pm.rebase_onto_base(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

    @app.post("/api/projects/{project_id}/reset")
    async def reset_project(project_id: int, request: Request) -> dict:
        """Discard uncommitted working-tree changes (commits are kept)."""
        current_user(request)
        try:
            result = await pm.reset_working_tree(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

    # 25 MB per uploaded file — generous for assets, small enough not to hurt
    # the Pi (uploads are buffered in memory before writing).
    _MAX_UPLOAD = 25 * 1024 * 1024

    async def _read_uploads(files: list[UploadFile]) -> tuple[list[tuple[str, bytes]], dict]:
        good: list[tuple[str, bytes]] = []
        errors: dict[str, str] = {}
        for f in files:
            name = f.filename or "file"
            data = await f.read()
            if len(data) > _MAX_UPLOAD:
                errors[name] = f"too large ({len(data)} bytes; limit {_MAX_UPLOAD})"
            else:
                good.append((name, data))
        return good, errors

    @app.post("/api/projects/{project_id}/upload")
    async def upload_project_files(
        project_id: int,
        request: Request,
        files: Annotated[list[UploadFile], File()],
        dest: Annotated[str, Form()] = "",
    ) -> dict:
        """Upload files into the working tree (committed straight away)."""
        current_user(request)
        good, errors = await _read_uploads(files)
        try:
            result = await pm.upload_files(project_id, good, dest=dest)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        result["errors"].update(errors)
        await bus.publish({"type": "projects_changed"})
        return result

    @app.post("/api/projects/{project_id}/chat-upload")
    async def upload_chat_files(
        project_id: int,
        request: Request,
        files: Annotated[list[UploadFile], File()],
    ) -> dict:
        """Stash chat attachments in .lab-uploads/ for the agent to read."""
        current_user(request)
        good, errors = await _read_uploads(files)
        try:
            saved = await pm.chat_uploads(project_id, good)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"saved": saved, "errors": errors}

    @app.get("/api/projects/{project_id}/archive")
    async def archive_project(project_id: int, request: Request) -> Response:
        """The working tree as a zip download (sync ignores excluded)."""
        current_user(request)
        try:
            name, data = await pm.archive(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.post("/api/projects/{project_id}/pull")
    async def pull_project(project_id: int, request: Request) -> dict:
        current_user(request)
        try:
            result = await pm.pull_base(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

    @app.post("/api/projects/{project_id}/push")
    async def push_project(project_id: int, request: Request) -> dict:
        current_user(request)
        try:
            result = await pm.push_base(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

    @app.post("/api/projects/{project_id}/fetch")
    async def fetch_project(project_id: int, request: Request) -> dict:
        current_user(request)
        try:
            result = await pm.fetch_base(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

    @app.get("/api/projects/{project_id}/branches")
    async def list_branches(project_id: int, request: Request) -> dict:
        current_user(request)
        try:
            return await pm.list_branches(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/projects/{project_id}/base")
    async def set_base(project_id: int, request: Request) -> dict:
        current_user(request)
        data = await request.json()
        name = (data.get("branch") or "").strip()
        try:
            result = await pm.set_base_branch(project_id, name)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

    @app.get("/api/projects/{project_id}/messages")
    async def messages(project_id: int, request: Request) -> list[dict]:
        current_user(request)
        return [
            {"role": m["role"], "content": m["content"]} for m in db.list_messages(conn, project_id)
        ]

    @app.post("/api/projects/{project_id}/clear")
    async def clear_chat(project_id: int, request: Request) -> dict:
        current_user(request)
        try:
            return await pm.clear_chat(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/projects/{project_id}/commits/{sha}/diff")
    async def commit_diff(project_id: int, sha: str, request: Request) -> dict:
        current_user(request)
        try:
            return await pm.commit_diff(project_id, sha)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/projects/{project_id}/tree")
    async def project_tree(project_id: int, request: Request, path: str = "") -> dict:
        current_user(request)
        try:
            return await pm.list_tree(project_id, path)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/projects/{project_id}/file")
    async def project_file(project_id: int, request: Request, path: str) -> dict:
        current_user(request)
        try:
            return await pm.read_file(project_id, path)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/projects/{project_id}/raw")
    async def project_raw(project_id: int, request: Request, path: str) -> FileResponse:
        current_user(request)
        try:
            target = await pm.raw_file(project_id, path)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return FileResponse(target)

    # --- Client mirrors: browse / fetch back / clean -----------------------
    # The file browser can show a connected platform client's mirror of a
    # project (run artifacts included) next to the lab's own working tree.

    def _mirror_project(project_id: int) -> str:
        """The mirror key for a project (its directory name) or 404."""
        try:
            return pm.project_root(project_id).name
        except ProjectError as exc:
            raise HTTPException(404, str(exc)) from exc

    async def _client_file(name: str, project: str, path: str) -> bytes:
        """One file's bytes from a client mirror, via the fetch frames."""
        try:
            fetched = await registry.fetch(name, project=project, paths=[path])
        except ClientError as exc:
            raise HTTPException(400, str(exc)) from exc
        if path in fetched["errors"]:
            raise HTTPException(400, fetched["errors"][path])
        if path not in fetched["files"]:
            raise HTTPException(400, f"client sent no data for {path!r}")
        return fetched["files"][path]

    @app.get("/api/projects/{project_id}/clients")
    async def project_clients(project_id: int, request: Request) -> list[dict]:
        """Connected clients that hold a mirror of this project."""
        current_user(request)
        project = _mirror_project(project_id)
        holders: list[dict] = []
        for c in registry.list():
            try:
                m = await registry.mirror(c["name"], project=project, timeout=15.0)
            except ClientError:
                continue  # raced a disconnect — omit rather than fail the list
            if m["exists"]:
                holders.append({"name": c["name"], "platform": c["platform"]})
        return holders

    @app.get("/api/projects/{project_id}/clients/{name}/mirror")
    async def client_mirror(project_id: int, name: str, request: Request) -> dict:
        """Flat file list of one client's mirror (the UI builds the tree)."""
        current_user(request)
        try:
            m = await registry.mirror(name, project=_mirror_project(project_id))
        except ClientError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"exists": m["exists"], "paths": sorted(m["manifest"])}

    @app.get("/api/projects/{project_id}/clients/{name}/file")
    async def client_file(project_id: int, name: str, request: Request, path: str) -> dict:
        current_user(request)
        data = await _client_file(name, _mirror_project(project_id), path)
        return _display_blob(path, data)

    @app.get("/api/projects/{project_id}/clients/{name}/raw")
    async def client_raw(project_id: int, name: str, request: Request, path: str) -> Response:
        current_user(request)
        data = await _client_file(name, _mirror_project(project_id), path)
        media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return Response(content=data, media_type=media_type)

    @app.post("/api/projects/{project_id}/clients/{name}/fetch")
    async def client_fetch(project_id: int, name: str, request: Request) -> dict:
        """Copy mirror files into the lab's working tree (web fetch_from_client)."""
        current_user(request)
        from platform_client import manifest

        data = await request.json()
        paths = [str(p) for p in data.get("paths") or []]
        if not paths:
            raise HTTPException(400, "paths required")
        try:
            root = pm.project_root(project_id)
        except ProjectError as exc:
            raise HTTPException(404, str(exc)) from exc
        try:
            fetched = await registry.fetch(name, project=root.name, paths=paths)
        except ClientError as exc:
            raise HTTPException(400, str(exc)) from exc
        written: list[str] = []
        errors = dict(fetched["errors"])
        for path, blob in fetched["files"].items():
            try:
                manifest.write_file(root, path, blob)
                written.append(path)
            except manifest.PathOutsideRoot:
                errors[path] = "path escapes the project root"
        return {"written": sorted(written), "errors": errors}

    @app.post("/api/projects/{project_id}/clients/{name}/clean")
    async def client_clean(project_id: int, name: str, request: Request) -> dict:
        """Delete the client's mirror of this project (files on the client)."""
        current_user(request)
        try:
            result = await registry.clean(name, project=_mirror_project(project_id))
        except ClientError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not result["ok"]:
            raise HTTPException(400, result.get("error") or "clean failed on the client")
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        if not websocket.session.get("user_id"):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        async with bus.subscribe() as events:
            pump = asyncio.create_task(_ws_pump(websocket, events))
            try:
                while True:
                    msg = json.loads(await websocket.receive_text())
                    if msg.get("type") == "message":
                        pid, text = msg.get("project_id"), msg.get("text")
                        if isinstance(pid, int) and isinstance(text, str) and text.strip():
                            asyncio.create_task(_run_turn(pm, bus, pid, text))
                        else:
                            await websocket.send_text(
                                json.dumps({"type": "error", "error": "bad message"})
                            )
                    elif msg.get("type") == "state":
                        projects = [_project_dict(pm, r) for r in pm.discover()]
                        await websocket.send_text(
                            json.dumps({"type": "state", "projects": projects})
                        )
            except WebSocketDisconnect:
                pass
            finally:
                pump.cancel()

    @app.websocket("/ws/client")
    async def ws_client(websocket: WebSocket) -> None:
        """Platform clients dial in here (cards/extension-clients.md).

        First frame must be a ``hello`` (with the shared token when one is
        configured); after that the registry owns the message routing.
        """
        await websocket.accept()
        try:
            hello = json.loads(await websocket.receive_text())
        except (WebSocketDisconnect, ValueError):
            return
        if hello.get("type") != "hello" or (
            config.client_token and hello.get("token") != config.client_token
        ):
            await websocket.close(code=1008)
            return

        async def send(message: dict) -> None:
            await websocket.send_text(json.dumps(message))

        name = registry.register(
            name=str(hello.get("name") or "client"),
            platform=str(hello.get("platform") or "unknown"),
            capabilities=list(hello.get("capabilities") or []),
            send=send,
        )
        await send({"type": "hello_ok", "name": name})
        await bus.publish({"type": "clients_changed"})
        try:
            while True:
                msg = json.loads(await websocket.receive_text())
                await registry.handle_message(name, msg)
        except (WebSocketDisconnect, ValueError):
            pass
        finally:
            registry.unregister(name)
            await bus.publish({"type": "clients_changed"})

    if static_dir is not None and Path(static_dir).is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app
