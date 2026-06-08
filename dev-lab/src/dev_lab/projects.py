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
from .workspace import Workspace, WorkspaceError


class ProjectError(RuntimeError):
    pass


def _derive_name(remote_url: str) -> str:
    """Repo name from a git URL: …/owner/foo.git or git@host:owner/foo -> 'foo'."""
    tail = remote_url.rstrip("/").replace(":", "/").rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", tail).strip("-._") or "project"


def _strip_credentials(url: str) -> str:
    """Drop any embedded ``user:pass@`` from an https URL.

    We never store or display a token-bearing URL; the token lives only on the
    project row, and is re-applied to the clone's ``origin`` on demand.
    """
    return re.sub(r"^(https://)[^/@]+@", r"\1", url)


def _authed_url(url: str, token: str | None) -> str:
    """Embed ``token`` as the basic-auth user in an https git URL.

    Returns the URL unchanged when there is no token, or for non-https (e.g.
    ``git@…`` ssh) remotes where a token does not apply. Any pre-existing
    credentials are stripped first so a token is never double-embedded.
    """
    if not token:
        return url
    clean = _strip_credentials(url)
    if clean.startswith("https://"):
        return clean.replace("https://", f"https://x-access-token:{token}@", 1)
    return clean


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

    def create(self, remote_url: str, github_token: str | None = None) -> sqlite3.Row:
        """Clone ``remote_url`` into labs/<repo-name> and register it.

        ``github_token`` is this project's own GitHub credential (there is no
        global token): it authenticates the clone and is persisted on the
        project's ``origin`` and row so later push/pull/fetch reuse it. Omit it
        for a public repo. The name is derived from the repo (``…/foo.git`` ->
        ``foo``); a collision gets a ``_2`` / ``_3`` / … suffix.
        """
        remote_url = _strip_credentials(remote_url.strip())
        if not remote_url:
            raise ProjectError("a git URL is required")
        token = (github_token or "").strip() or None
        name = self._unique_name(_derive_name(remote_url))
        dest = self.labs_dir / name

        clone = subprocess.run(
            ["git", "clone", "--quiet", _authed_url(remote_url, token), str(dest)],
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

        pid = db.create_project(
            self.conn, name=name, path=str(dest), remote_url=remote_url, github_token=token
        )
        return db.get_project(self.conn, pid)

    async def set_token(self, project_id: int, token: str | None) -> dict:
        """Set (or clear, with empty ``token``) a project's GitHub credential.

        Persists the token on the project row and re-points the clone's
        ``origin`` at the (clean) remote with the new token embedded, so the
        agent's pushes and the manager's push/pull/fetch pick it up immediately.
        Updating the row still succeeds if the clone is missing or broken.
        """
        row = db.get_project(self.conn, project_id)
        if row is None:
            raise ProjectError(f"no such project: {project_id}")
        token = (token or "").strip() or None
        async with self.lock(project_id):
            db.set_project_token(self.conn, project_id, token)
            ws = Workspace(Path(row["path"]))
            try:
                ws.ensure_repo()
                base = row["remote_url"] or _strip_credentials(ws.remote_url() or "")
                if base:
                    ws.set_remote_url(_authed_url(base, token))
            except WorkspaceError:
                pass  # no/broken clone — the row is updated; origin is fixed on next clone
        return {"has_token": token is not None}

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

    def effective_base(self, row: sqlite3.Row) -> str | None:
        """Effective base for display: configured override, else repo default.

        Best-effort — returns the configured value (or ``None``) when the
        workspace can't be inspected, so listing projects never fails on a
        missing or broken clone.
        """
        configured = row["base_branch"]
        if configured:
            return configured
        try:
            ws = Workspace(Path(row["path"]))
            ws.ensure_repo()
            return ws.default_branch()
        except WorkspaceError:
            return None

    async def list_branches(self, project_id: int) -> dict:
        """Available branch names plus the project's current effective base."""
        row = db.get_project(self.conn, project_id)
        if row is None:
            raise ProjectError(f"no such project: {project_id}")
        ws = Workspace(Path(row["path"]))
        async with self.lock(project_id):
            ws.ensure_repo()
            branches = ws.list_branches()
            base = self._base_branch(ws, project_id)
        return {"branches": branches, "base": base}

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

    async def merge_base_into_branch(self, project_id: int) -> dict:
        """Merge a project's base branch into its chat branch (locally) — a pull.

        The mirror of ``merge_to_base``: instead of landing the chat branch on
        base, it brings base's commits *into* the chat branch so the session
        keeps working on an up-to-date branch. Checks out the chat branch and
        merges base into it (``workspace.merge`` aborts and raises on conflict);
        the working tree is left on the chat branch, ready for the next turn.
        """
        session = self.open(project_id)
        ws = session.workspace
        branch = session.branch
        async with self.lock(project_id):
            ws.ensure_repo()
            if not ws.branch_exists(branch):
                raise ProjectError("no work branch yet — start a chat first")
            base = self._base_branch(ws, project_id)
            if base == branch:
                raise ProjectError("the chat branch is the base branch; nothing to merge")
            merged = ws.merge(branch, base, message=f"Merge {base} into {branch}")
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

    async def fetch_base(self, project_id: int) -> dict:
        """Fetch the project's base branch from origin (updates the tracking ref only)."""
        session = self.open(project_id)
        ws = session.workspace
        async with self.lock(project_id):
            ws.ensure_repo()
            base = self._base_branch(ws, project_id)
            commit = ws.fetch(base)
        return {"base": base, "commit": commit}

    def _workspace(self, project_id: int) -> Workspace:
        """A bare ``Workspace`` for a project (read-only inspection helpers)."""
        row = db.get_project(self.conn, project_id)
        if row is None:
            raise ProjectError(f"no such project: {project_id}")
        return Workspace(Path(row["path"]))

    async def commit_diff(self, project_id: int, sha: str) -> dict:
        """Subject + unified patch for a single commit in a project."""
        ws = self._workspace(project_id)
        async with self.lock(project_id):
            ws.ensure_repo()
            subject = ws.commit_subject(sha)
            diff = ws.commit_diff(sha)
        return {"sha": sha, "subject": subject, "diff": diff}

    async def list_tree(self, project_id: int, path: str = "") -> dict:
        """One directory level of a project's working tree (repo browser).

        Lists what's actually checked out on disk, and annotates each entry with
        its status relative to the project's base branch (``new`` / ``modified``
        / a dir containing changes) so divergence is visible rather than
        silent. Also returns the current ``branch``, the ``base`` it's compared
        against, and ``missing`` — the count of files that exist on base but not
        in the checkout (e.g. when parked on a chat branch cut before they were
        added) — so the UI can explain why the listing may look short.
        """
        ws = self._workspace(project_id)
        async with self.lock(project_id):
            ws.ensure_repo()
            entries = ws.list_tree(path)
            base = self._base_branch(ws, project_id)
            branch = ws.current_branch()
            status = ws.diff_status(base)
        for entry in entries:
            if entry["type"] == "file":
                entry["status"] = status.get(entry["path"])
            else:
                prefix = entry["path"] + "/"
                entry["status"] = (
                    "modified"
                    if any(p.startswith(prefix) and s != "deleted" for p, s in status.items())
                    else None
                )
        missing = sum(1 for s in status.values() if s == "deleted")
        return {
            "path": path,
            "entries": entries,
            "branch": branch,
            "base": base,
            "missing": missing,
        }

    async def read_file(self, project_id: int, path: str) -> dict:
        """Read one working-tree file from a project for display."""
        ws = self._workspace(project_id)
        async with self.lock(project_id):
            ws.ensure_repo()
            return ws.read_file(path)

    async def raw_file(self, project_id: int, path: str) -> Path:
        """Resolve a project working-tree file to its absolute path.

        For raw byte streaming (images and other binaries the browser renders
        directly, plus images referenced by relative paths inside markdown).
        """
        ws = self._workspace(project_id)
        async with self.lock(project_id):
            ws.ensure_repo()
            return ws.file_path(path)

    async def clear_chat(self, project_id: int) -> dict:
        """Erase a project's chat history and reset its agent context.

        The web equivalent of ``/clear``: wipes the persisted conversation,
        forgets the resumed SDK session, and drops the cached ``LabSession`` so
        the next turn starts from a clean context. The branch and working tree
        are left untouched — only the conversation is reset.
        """
        if db.get_project(self.conn, project_id) is None:
            raise ProjectError(f"no such project: {project_id}")
        async with self.lock(project_id):
            db.clear_messages(self.conn, project_id)
            db.clear_session(self.conn, project_id)
            # Drop the cached session so open() rebuilds it from the now-reset
            # row (no last_session_id) instead of resuming the old context.
            self._sessions.pop(project_id, None)
        return {"cleared": True}

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
