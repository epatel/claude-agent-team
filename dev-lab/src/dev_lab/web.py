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
import uuid
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


def _project_dict(
    pm: ProjectManager, row: sqlite3.Row, conn: sqlite3.Connection, *, running: bool = False
) -> dict:
    # Owner's username — shown to super-users (who see everyone's projects);
    # for everyone else it is only ever their own name. None = ownerless row.
    owner = auth.get_user(conn, row["owner_id"]) if row["owner_id"] else None
    return {
        "id": row["id"],
        "name": row["name"],
        "branch": row["branch"],
        "base_branch": pm.effective_base(row),
        # Whether a GitHub token is configured — never the token itself.
        "has_token": bool(row["github_token"]),
        # The model this project will run with (its override, else the lab default).
        "model": pm.effective_model(row),
        "owner": owner["username"] if owner else None,
        # Whether a turn is in flight right now — lets a (re)loaded console
        # restore the running status/dot instead of guessing from events.
        "running": running,
    }


async def _run_turn(pm: ProjectManager, bus: EventBus, project_id: int, text: str) -> None:
    async def on_event(event: dict) -> None:
        await bus.publish({**event, "project_id": project_id})

    await bus.publish({"type": "turn_running", "project_id": project_id, "text": text})
    try:
        result = await pm.run_turn(project_id, text, on_event=on_event)
    except asyncio.CancelledError:
        # The stop button: the SDK call (and its CLI subprocess) is torn down by
        # cancellation; record a marker so the transcript explains itself on
        # reload, and let the UI reset via its own event.
        db.record_message(
            pm.conn, project_id=project_id, role="assistant", content="[stopped by user]"
        )
        await bus.publish({"type": "turn_stopped", "project_id": project_id})
        return
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


async def _ws_pump(ws: WebSocket, events: asyncio.Queue, allowed=None) -> None:
    """Forward bus events to one console socket.

    ``allowed(event) -> bool`` filters project-scoped events so a user's
    console never receives another user's chat/tool stream (strict per-user
    projects); events without a project_id (clients_changed, projects_changed)
    pass through — they only poke the UI to re-fetch filtered endpoints.
    """
    try:
        while True:
            event = await events.get()
            if allowed is not None and not allowed(event):
                continue
            await ws.send_text(json.dumps(event))
    except (WebSocketDisconnect, RuntimeError):
        return


def _ensure_lab_id(state_dir: Path) -> str:
    """A stable id for this lab, minted once into ``<labs>/.dev-lab/lab-id``.

    Sent to platform clients in ``hello_ok`` so they namespace their mirrors
    per lab — two labs sharing one client machine must not collide on
    same-named projects (see cards/extension-clients.md).
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    id_file = state_dir / "lab-id"
    if id_file.exists():
        return id_file.read_text().strip()
    lab_id = uuid.uuid4().hex[:12]
    id_file.write_text(lab_id)
    return lab_id


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

    def _clients_busy_changed() -> None:
        # A client's in-flight task count changed — nudge consoles to re-fetch
        # /api/clients (busy badges). Fire-and-forget on the running loop.
        try:
            asyncio.get_running_loop().create_task(
                bus.publish({"type": "clients_changed"})
            )
        except RuntimeError:
            pass  # no loop (e.g. unit-test construction) — nothing to notify

    registry = ClientRegistry(on_change=_clients_busy_changed)
    app.state.lab_id = _ensure_lab_id(Path(labs_dir) / ".dev-lab")
    app.state.registry = registry  # reachable from tests
    pm = ProjectManager(labs_dir=labs_dir, config=config, conn=conn, client_registry=registry)
    pm.discover()
    # The in-flight turn per project, so a console can stop it (turns within a
    # project serialize on the pm lock, so one task per project suffices).
    running_turns: dict[int, asyncio.Task] = {}

    def project_dict(row: sqlite3.Row) -> dict:
        return _project_dict(pm, row, conn, running=row["id"] in running_turns)

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

    def _can_access(user: sqlite3.Row, project: sqlite3.Row) -> bool:
        """Strict per-user projects: owners see theirs, supers see everything.

        ``owner_id`` NULL (pre-migration rows, checkouts dropped into labs/)
        means "no owner" — visible to super-users only.
        """
        return bool(user["is_super"]) or project["owner_id"] == user["id"]

    def _require_project(request: Request, project_id: int) -> sqlite3.Row:
        """Auth + ownership gate for project-scoped endpoints.

        An existing-but-foreign project 404s (not 403) so the API doesn't
        confirm which ids exist to other users.
        """
        user = current_user(request)
        row = db.get_project(conn, project_id)
        if row is None or not _can_access(user, row):
            raise HTTPException(404, f"no such project: {project_id}")
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
        user = current_user(request)
        return [project_dict(r) for r in pm.discover() if _can_access(user, r)]

    @app.post("/api/projects")
    async def create_project(request: Request) -> dict:
        """Clone ``remote_url`` — or, given only ``name``, git-init a blank repo."""
        user = current_user(request)
        data = await request.json()
        remote_url = (data.get("remote_url") or "").strip()
        name = (data.get("name") or "").strip()
        github_token = (data.get("github_token") or "").strip()
        model = (data.get("model") or "").strip()
        try:
            if remote_url:
                row = pm.create(
                    remote_url, github_token=github_token, model=model, owner_id=user["id"]
                )
            elif name:
                row = pm.create_blank(name, model=model, owner_id=user["id"])
            else:
                raise ProjectError("a git URL (clone) or a project name (new repo) is required")
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return project_dict(row)

    @app.delete("/api/projects/{project_id}")
    async def remove_project(project_id: int, request: Request) -> dict:
        """Remove the lab's copy of a project and clean client mirrors."""
        _require_project(request, project_id)
        try:
            result = await pm.remove(project_id)
        except ProjectError as exc:
            raise HTTPException(404, str(exc)) from exc
        except WorkspaceError as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

    @app.post("/api/projects/{project_id}/token")
    async def set_token(project_id: int, request: Request) -> dict:
        _require_project(request, project_id)
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
        _require_project(request, project_id)
        data = await request.json()
        model = (data.get("model") or "").strip()
        try:
            result = await pm.set_model(project_id, model)
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

    # --- Agent tab: project prompt, MCP servers, skills ---------------------

    @app.get("/api/projects/{project_id}/agent")
    async def get_agent_config(project_id: int, request: Request) -> dict:
        _require_project(request, project_id)
        row = db.get_project(conn, project_id)
        try:
            skills = await pm.list_skills(project_id)
        except (ProjectError, WorkspaceError):
            skills = []
        return {
            "agent_prompt": row["agent_prompt"] or "",
            "mcp_servers": row["mcp_servers"] or "",
            "skills": skills,
        }

    @app.post("/api/projects/{project_id}/agent")
    async def set_agent_config(project_id: int, request: Request) -> dict:
        """Save the project prompt and/or MCP servers (key present = set it)."""
        _require_project(request, project_id)
        data = await request.json()
        out: dict = {}
        try:
            if "agent_prompt" in data:
                out.update(await pm.set_agent_prompt(project_id, str(data["agent_prompt"])))
            if "mcp_servers" in data:
                out.update(await pm.set_mcp_servers(project_id, str(data["mcp_servers"])))
        except ProjectError as exc:
            raise HTTPException(400, str(exc)) from exc
        return out

    @app.post("/api/projects/{project_id}/skills")
    async def add_skills(
        project_id: int,
        request: Request,
        files: Annotated[list[UploadFile], File()],
    ) -> dict:
        """Install uploaded SKILL.md files; each names itself via frontmatter."""
        _require_project(request, project_id)
        good, errors = await _read_uploads(files)
        added: list[str] = []
        commit = None
        for fname, data in good:
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError:
                errors[fname] = "not a UTF-8 text file"
                continue
            try:
                result = await pm.add_skill(project_id, content)
            except (ProjectError, WorkspaceError) as exc:
                errors[fname] = str(exc)
                continue
            added.append(result["name"])
            commit = result["commit"] or commit
        return {"added": added, "errors": errors, "commit": commit}

    @app.delete("/api/projects/{project_id}/skills/{name}")
    async def remove_skill(project_id: int, name: str, request: Request) -> dict:
        _require_project(request, project_id)
        try:
            return await pm.remove_skill(project_id, name)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/projects/{project_id}/merge")
    async def merge_project(project_id: int, request: Request) -> dict:
        _require_project(request, project_id)
        try:
            result = await pm.merge_to_base(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

    @app.post("/api/projects/{project_id}/rebase")
    async def rebase_project(project_id: int, request: Request) -> dict:
        """Rebase the chat branch onto base; conflicts come back as data."""
        _require_project(request, project_id)
        try:
            result = await pm.rebase_onto_base(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

    @app.post("/api/projects/{project_id}/reset")
    async def reset_project(project_id: int, request: Request) -> dict:
        """Discard uncommitted working-tree changes (commits are kept)."""
        _require_project(request, project_id)
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
        _require_project(request, project_id)
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
        _require_project(request, project_id)
        good, errors = await _read_uploads(files)
        try:
            saved = await pm.chat_uploads(project_id, good)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"saved": saved, "errors": errors}

    @app.get("/api/projects/{project_id}/archive")
    async def archive_project(project_id: int, request: Request) -> Response:
        """The working tree as a zip download (sync ignores excluded)."""
        _require_project(request, project_id)
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
        _require_project(request, project_id)
        try:
            result = await pm.pull_base(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

    @app.post("/api/projects/{project_id}/push")
    async def push_project(project_id: int, request: Request) -> dict:
        _require_project(request, project_id)
        try:
            result = await pm.push_base(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

    @app.post("/api/projects/{project_id}/fetch")
    async def fetch_project(project_id: int, request: Request) -> dict:
        _require_project(request, project_id)
        try:
            result = await pm.fetch_base(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc
        await bus.publish({"type": "projects_changed"})
        return result

    @app.get("/api/projects/{project_id}/branches")
    async def list_branches(project_id: int, request: Request) -> dict:
        _require_project(request, project_id)
        try:
            return await pm.list_branches(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/projects/{project_id}/status")
    async def repo_status(project_id: int, request: Request) -> dict:
        _require_project(request, project_id)
        try:
            return await pm.repo_status(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/projects/{project_id}/base")
    async def set_base(project_id: int, request: Request) -> dict:
        _require_project(request, project_id)
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
        _require_project(request, project_id)
        return [
            {
                "role": m["role"],
                "content": m["content"],
                "kind": m["kind"],
                "meta": m["meta"],  # JSON string or None; the client parses it
            }
            for m in db.list_messages(conn, project_id)
        ]

    @app.post("/api/projects/{project_id}/clear")
    async def clear_chat(project_id: int, request: Request) -> dict:
        _require_project(request, project_id)
        try:
            return await pm.clear_chat(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/projects/{project_id}/commits/{sha}/diff")
    async def commit_diff(project_id: int, sha: str, request: Request) -> dict:
        _require_project(request, project_id)
        try:
            return await pm.commit_diff(project_id, sha)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/projects/{project_id}/diff")
    async def base_diff(project_id: int, request: Request) -> dict:
        """The full patch the chat branch adds on top of base (base...HEAD)."""
        _require_project(request, project_id)
        try:
            return await pm.base_diff(project_id)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/projects/{project_id}/tree")
    async def project_tree(project_id: int, request: Request, path: str = "") -> dict:
        _require_project(request, project_id)
        try:
            return await pm.list_tree(project_id, path)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/projects/{project_id}/file")
    async def project_file(project_id: int, request: Request, path: str) -> dict:
        _require_project(request, project_id)
        try:
            return await pm.read_file(project_id, path)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/projects/{project_id}/path")
    async def delete_path(project_id: int, request: Request, path: str) -> dict:
        """Delete a file or directory from the working tree (committed)."""
        _require_project(request, project_id)
        try:
            return await pm.delete_path(project_id, path)
        except (ProjectError, WorkspaceError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/projects/{project_id}/raw")
    async def project_raw(project_id: int, request: Request, path: str) -> FileResponse:
        _require_project(request, project_id)
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
        _require_project(request, project_id)
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
        _require_project(request, project_id)
        try:
            m = await registry.mirror(name, project=_mirror_project(project_id))
        except ClientError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"exists": m["exists"], "paths": sorted(m["manifest"])}

    @app.get("/api/projects/{project_id}/clients/{name}/file")
    async def client_file(project_id: int, name: str, request: Request, path: str) -> dict:
        _require_project(request, project_id)
        data = await _client_file(name, _mirror_project(project_id), path)
        return _display_blob(path, data)

    @app.get("/api/projects/{project_id}/clients/{name}/raw")
    async def client_raw(project_id: int, name: str, request: Request, path: str) -> Response:
        _require_project(request, project_id)
        data = await _client_file(name, _mirror_project(project_id), path)
        media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return Response(content=data, media_type=media_type)

    @app.post("/api/projects/{project_id}/clients/{name}/fetch")
    async def client_fetch(project_id: int, name: str, request: Request) -> dict:
        """Copy mirror files into the lab's working tree (web fetch_from_client)."""
        _require_project(request, project_id)
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
        _require_project(request, project_id)
        try:
            result = await registry.clean(name, project=_mirror_project(project_id))
        except ClientError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not result["ok"]:
            raise HTTPException(400, result.get("error") or "clean failed on the client")
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        uid = websocket.session.get("user_id")
        user = auth.get_user(conn, uid) if uid else None
        if user is None or user["blocked"]:
            await websocket.close(code=1008)
            return

        def ws_can_access(project_id: object) -> bool:
            row = db.get_project(conn, project_id) if isinstance(project_id, int) else None
            return row is not None and _can_access(user, row)

        def event_allowed(event: dict) -> bool:
            pid = event.get("project_id")
            return pid is None or ws_can_access(pid)

        await websocket.accept()
        async with bus.subscribe() as events:
            pump = asyncio.create_task(_ws_pump(websocket, events, allowed=event_allowed))
            try:
                while True:
                    msg = json.loads(await websocket.receive_text())
                    if msg.get("type") == "message":
                        pid, text = msg.get("project_id"), msg.get("text")
                        if (
                            isinstance(pid, int)
                            and isinstance(text, str)
                            and text.strip()
                            and ws_can_access(pid)
                        ):
                            task = asyncio.create_task(_run_turn(pm, bus, pid, text))
                            running_turns[pid] = task
                            task.add_done_callback(
                                lambda t, p=pid: running_turns.pop(p, None)
                                if running_turns.get(p) is t
                                else None
                            )
                        else:
                            await websocket.send_text(
                                json.dumps({"type": "error", "error": "bad message"})
                            )
                    elif msg.get("type") == "stop":
                        pid = msg.get("project_id")
                        if isinstance(pid, int) and ws_can_access(pid):
                            task = running_turns.get(pid)
                            if task is not None:
                                task.cancel()
                    elif msg.get("type") == "state":
                        projects = [
                            project_dict(r) for r in pm.discover() if _can_access(user, r)
                        ]
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
            mcp_servers=[str(s) for s in (hello.get("mcp_servers") or [])],
            chunked_files=bool(hello.get("chunked_files")),
        )
        await send({"type": "hello_ok", "name": name, "lab": app.state.lab_id})
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
