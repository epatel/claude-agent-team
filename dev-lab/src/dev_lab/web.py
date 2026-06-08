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
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from . import auth, db
from .config import Config
from .events import EventBus
from .projects import ProjectError, ProjectManager
from .workspace import WorkspaceError


def _project_dict(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "name": row["name"], "branch": row["branch"]}


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
        return user

    @app.post("/api/register")
    async def register(request: Request) -> dict:
        data = await request.json()
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        if not username or not password:
            raise HTTPException(400, "username and password required")
        try:
            uid = auth.create_user(conn, username, password)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        request.session["user_id"] = uid
        return {"username": username}

    @app.post("/api/login")
    async def login(request: Request) -> dict:
        data = await request.json()
        user = auth.verify_user(conn, data.get("username", ""), data.get("password", ""))
        if user is None:
            raise HTTPException(401, "invalid credentials")
        request.session["user_id"] = user["id"]
        return {"username": user["username"]}

    @app.post("/api/logout")
    async def logout(request: Request) -> dict:
        request.session.clear()
        return {"ok": True}

    @app.get("/api/me")
    async def me(request: Request) -> dict:
        return {"username": current_user(request)["username"]}

    @app.get("/api/projects")
    async def list_projects(request: Request) -> list[dict]:
        current_user(request)
        return [_project_dict(r) for r in pm.discover()]

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
        return _project_dict(row)

    @app.post("/api/projects/{project_id}/merge")
    async def merge_project(project_id: int, request: Request) -> dict:
        current_user(request)
        try:
            result = await pm.merge_to_base(project_id)
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

    @app.get("/api/projects/{project_id}/messages")
    async def messages(project_id: int, request: Request) -> list[dict]:
        current_user(request)
        return [
            {"role": m["role"], "content": m["content"]} for m in db.list_messages(conn, project_id)
        ]

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
                        projects = [_project_dict(r) for r in pm.discover()]
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
