"""Multi-project manager: the ``labs/`` directory of checked-out projects.

Each project is its own git clone under ``labs/``, opened and talked to as a
separate Claude agent/context (own branch, own resumed session). Projects are
isolated by construction (separate working dirs), so chat across different
projects runs concurrently; turns within a project serialize on a per-project
lock.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import subprocess
from pathlib import Path

from . import db
from .agent import run_task as _run_task
from .config import Config
from .session import LabSession, RunTask, TurnResult

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ProjectError(RuntimeError):
    pass


class ProjectManager:
    def __init__(
        self,
        *,
        labs_dir: str | Path,
        config: Config,
        conn: sqlite3.Connection,
        run_task: RunTask = _run_task,
    ) -> None:
        self.labs_dir = Path(labs_dir)
        self.config = config
        self.conn = conn
        self._run_task = run_task
        self._sessions: dict[int, LabSession] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self.labs_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _is_git_repo(path: Path) -> bool:
        return (path / ".git").exists()

    def _authed_url(self, url: str) -> str:
        token = self.config.github_token
        if token and url.startswith("https://github.com/"):
            return url.replace("https://", f"https://x-access-token:{token}@", 1)
        return url

    def discover(self) -> list[sqlite3.Row]:
        """Register any git checkout sitting in labs/ that isn't recorded yet."""
        for child in sorted(self.labs_dir.iterdir()):
            if child.is_dir() and not child.name.startswith(".") and self._is_git_repo(child):
                if db.get_project_by_name(self.conn, child.name) is None:
                    db.create_project(self.conn, name=child.name, path=str(child))
        return db.list_projects(self.conn)

    def create(self, name: str, remote_url: str) -> sqlite3.Row:
        """Clone ``remote_url`` into labs/<name> and register it."""
        if not _NAME_RE.match(name):
            raise ProjectError(f"invalid project name: {name!r} (use letters, digits, . _ -)")
        if db.get_project_by_name(self.conn, name) is not None:
            raise ProjectError(f"project already exists: {name}")
        dest = self.labs_dir / name
        if dest.exists():
            raise ProjectError(f"path already exists: {dest}")

        clone = subprocess.run(
            ["git", "clone", "--quiet", self._authed_url(remote_url), str(dest)],
            capture_output=True,
            text=True,
        )
        if clone.returncode != 0:
            raise ProjectError(f"git clone failed: {clone.stderr.strip()}")

        # Give it a commit identity so the agent's commits work.
        subprocess.run(["git", "-C", str(dest), "config", "user.name", "Dev Lab"], check=False)
        subprocess.run(
            ["git", "-C", str(dest), "config", "user.email", "lab@local"], check=False
        )

        pid = db.create_project(self.conn, name=name, path=str(dest), remote_url=remote_url)
        return db.get_project(self.conn, pid)

    def lock(self, project_id: int) -> asyncio.Lock:
        return self._locks.setdefault(project_id, asyncio.Lock())

    def open(self, project_id: int) -> LabSession:
        """Return the cached session for a project, restoring branch/context."""
        if project_id in self._sessions:
            return self._sessions[project_id]
        row = db.get_project(self.conn, project_id)
        if row is None:
            raise ProjectError(f"no such project: {project_id}")
        session = LabSession(
            repo_path=row["path"],
            config=self.config,
            branch=row["branch"] or None,
            session_id=row["last_session_id"],
            branch_started=bool(row["branch"]),
            run_task=self._run_task,
        )
        self._sessions[project_id] = session
        return session

    async def run_turn(self, project_id: int, message: str, *, on_event=None) -> TurnResult:
        """Run one chat turn for a project: persist message, run, persist state."""
        session = self.open(project_id)
        db.record_message(self.conn, project_id=project_id, role="user", content=message)
        async with self.lock(project_id):
            result = await session.run_turn(message, on_event=on_event)
        db.update_project(
            self.conn, project_id, branch=session.branch, last_session_id=session.session_id
        )
        db.record_message(
            self.conn, project_id=project_id, role="assistant", content=result.agent.summary
        )
        return result
