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
from .workspace import Workspace


class ProjectError(RuntimeError):
    pass


def _derive_name(remote_url: str) -> str:
    """Repo name from a git URL: …/owner/foo.git or git@host:owner/foo -> 'foo'."""
    tail = remote_url.rstrip("/").replace(":", "/").rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", tail).strip("-._") or "project"


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

    def _unique_name(self, base: str) -> str:
        """``base`` if free, else ``base_2`` / ``base_3`` / … (avoids db + dir clashes)."""
        def taken(name: str) -> bool:
            return (
                db.get_project_by_name(self.conn, name) is not None
                or (self.labs_dir / name).exists()
            )

        if not taken(base):
            return base
        n = 2
        while taken(f"{base}_{n}"):
            n += 1
        return f"{base}_{n}"

    def create(self, remote_url: str) -> sqlite3.Row:
        """Clone ``remote_url`` into labs/<repo-name> and register it.

        The name is derived from the repo (``…/foo.git`` -> ``foo``); a collision
        gets a ``_2`` / ``_3`` / … suffix.
        """
        remote_url = remote_url.strip()
        if not remote_url:
            raise ProjectError("a git URL is required")
        name = self._unique_name(_derive_name(remote_url))
        dest = self.labs_dir / name

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
            base_branch=row["base_branch"] or None,
            session_id=row["last_session_id"],
            branch_started=bool(row["branch"]),
            run_task=self._run_task,
        )
        self._sessions[project_id] = session
        return session

    def _base_branch(self, ws, project_id: int) -> str:
        """The project's configured base branch, or the repo default when unset."""
        row = db.get_project(self.conn, project_id)
        configured = row["base_branch"] if row is not None else None
        return configured or ws.default_branch()

    async def set_base_branch(self, project_id: int, name: str) -> dict:
        """Set the branch new chat threads are cut from (must already exist).

        Only affects newly-started chat threads — an in-progress session keeps
        the base it was started on. Validates against a fresh ``Workspace`` (not
        ``open()``) so it doesn't cache a session that would shadow the new base.
        """
        row = db.get_project(self.conn, project_id)
        if row is None:
            raise ProjectError(f"no such project: {project_id}")
        ws = Workspace(Path(row["path"]))
        async with self.lock(project_id):
            ws.ensure_repo()
            if name not in ws.list_branches():
                raise ProjectError(f"no such branch: {name}")
            db.update_project(self.conn, project_id, base_branch=name)
        return {"base_branch": name}

    async def merge_to_base(self, project_id: int) -> dict:
        """Merge a project's chat branch into its base branch (locally)."""
        session = self.open(project_id)
        ws = session.workspace
        branch = session.branch
        async with self.lock(project_id):
            ws.ensure_repo()
            if not ws.branch_exists(branch):
                raise ProjectError("no work to merge yet — start a chat first")
            base = self._base_branch(ws, project_id)
            if base == branch:
                raise ProjectError("the chat branch is the base branch; nothing to merge")
            merged = ws.merge(base, branch, message=f"Merge {branch} into {base}")
            ws.checkout(branch)  # restore so the session can keep going
        return {"base": base, "branch": branch, "commit": merged}

    async def pull_base(self, project_id: int) -> dict:
        """Pull the project's base branch from origin (locally, fast-forward only)."""
        session = self.open(project_id)
        ws = session.workspace
        async with self.lock(project_id):
            ws.ensure_repo()
            base = self._base_branch(ws, project_id)
            commit = ws.pull(base)
        return {"base": base, "commit": commit}

    async def push_base(self, project_id: int) -> dict:
        """Push the project's base branch to origin."""
        session = self.open(project_id)
        ws = session.workspace
        async with self.lock(project_id):
            ws.ensure_repo()
            base = self._base_branch(ws, project_id)
            commit = ws.push(base)
        return {"base": base, "commit": commit}

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
