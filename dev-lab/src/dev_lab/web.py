"""FastAPI web console: login, project list, and per-project chat over WebSocket.

The primary control surface for v2. Each project (a clone under ``labs/``) is its
own Claude agent/context; the WS streams turn lifecycle, agent text, and tool
calls, and persists the conversation per project.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from . import auth, db
from .config import Config
from .events import EventBus
from .projects import ProjectError, ProjectManager
from .workspace import WorkspaceError


def _project_dict(pm: ProjectManager, row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "branch": row["branch"],
        "base_branch": pm.effective_base(row),
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
    pm = ProjectManager(labs_dir=labs_dir, config=config, conn=conn)
    bus = bus or EventBus()
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

    @app.get("/api/projects")
    async def list_projects(request: Request) -> list[dict]:
        current_user(request)
        return [_project_dict(pm, r) for r in pm.discover()]

    @app.post("/api/projects")
    async def create_project(request: Request) -> dict:
        current_user(request)
        data = await request.json()
        remote_url = (data.get("remote_url") or "").strip()
        try:
            row = pm.create(remote_url)
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return _project_dict(pm, row)

    @app.post("/api/projects/{project_id}/merge")
    async def merge_project(project_id: int, request: Request) -> dict:
        current_user(request)
        try:
            result = await pm.merge_to_base(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

    @app.post("/api/projects/{project_id}/merge-base")
    async def merge_base_project(project_id: int, request: Request) -> dict:
        current_user(request)
        try:
            result = await pm.merge_base_into_branch(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

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

    if static_dir is not None and Path(static_dir).is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app
