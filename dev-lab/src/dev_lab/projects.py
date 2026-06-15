"""Multi-project manager: the ``labs/`` directory of checked-out projects.

Each project is its own git clone under ``labs/``, opened and talked to as a
separate Claude agent/context (own branch, own resumed session). Projects are
isolated by construction (separate working dirs), so chat across different
projects runs concurrently; turns within a project serialize on a per-project
lock.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import shutil
import sqlite3
import subprocess
import uuid
import zipfile
from dataclasses import replace
from pathlib import Path, PurePosixPath

from . import db
from .agent import run_task as _run_task
from .clients import ClientError
from .config import KNOWN_MODEL_IDS, Config
from .session import LabSession, RunTask, TurnResult
from .workspace import Workspace, WorkspaceConflict, WorkspaceError


class ProjectError(RuntimeError):
    pass


# A blank-project name: one safe path segment, no leading dot (so no ".git",
# no hidden dirs), bounded length.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


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


def _skill_frontmatter(content: str) -> tuple[str | None, str]:
    """(name, description) from a SKILL.md YAML frontmatter block.

    The skill's identity lives in its frontmatter ``name:`` (that is what the
    agent sees), so an uploaded file names itself — no separate name field.
    """
    m = re.match(r"\s*---\s*\n(.*?)\n\s*---", content, re.DOTALL)
    if not m:
        return None, ""
    name, description = None, ""
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "name":
            name = value.strip().strip("\"'") or None
        elif key.strip() == "description":
            description = value.strip().strip("\"'")
    return name, description


def _parse_mcp_servers(raw: str | None) -> dict | None:
    """Parse a project's stored MCP-servers JSON; tolerate bad rows as None."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


class ProjectManager:
    def __init__(
        self,
        *,
        labs_dir: str | Path,
        config: Config,
        conn: sqlite3.Connection,
        run_task: RunTask = _run_task,
        client_registry=None,
    ) -> None:
        self.labs_dir = Path(labs_dir)
        self.config = config
        self.conn = conn
        self._run_task = run_task
        self._client_registry = client_registry
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

    def effective_model(self, row: sqlite3.Row) -> str:
        """The model a project will run with: its own override, else the lab default."""
        return row["model"] or self.config.model

    def create_blank(
        self, name: str, model: str | None = None, owner_id: int | None = None
    ) -> sqlite3.Row:
        """git-init a brand-new empty project under labs/<name> (no remote).

        For starting something from scratch in the lab. ``name`` is chosen
        explicitly (one path segment, ``_NAME_RE``), so a collision is an error
        rather than a ``_2`` suffix. The repo gets a seed README committed on
        ``main`` so a base branch exists for chat sessions to cut from; a
        remote can be attached later from a chat turn if it ever needs one.
        """
        name = (name or "").strip()
        if not _NAME_RE.match(name):
            raise ProjectError(
                "project name must be letters/digits/._- (start with a letter or digit)"
            )
        model = (model or "").strip() or None
        if model is not None and model not in KNOWN_MODEL_IDS:
            raise ProjectError(f"unknown model: {model}")
        dest = self.labs_dir / name
        if dest.exists() or db.get_project_by_name(self.conn, name) is not None:
            raise ProjectError(f"a project named {name!r} already exists")

        init = subprocess.run(
            ["git", "init", "--quiet", "-b", "main", str(dest)],
            capture_output=True,
            text=True,
        )
        if init.returncode != 0:
            raise ProjectError(f"git init failed: {init.stderr.strip()}")
        # Same commit identity a cloned project gets, so agent commits work.
        subprocess.run(["git", "-C", str(dest), "config", "user.name", "Dev Lab"], check=False)
        subprocess.run(
            ["git", "-C", str(dest), "config", "user.email", "lab@local"], check=False
        )
        (dest / "README.md").write_text(f"# {name}\n")
        subprocess.run(["git", "-C", str(dest), "add", "-A"], check=False)
        commit = subprocess.run(
            ["git", "-C", str(dest), "commit", "--quiet", "-m", "Initial commit"],
            capture_output=True,
            text=True,
        )
        if commit.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise ProjectError(f"initial commit failed: {commit.stderr.strip()}")

        pid = db.create_project(
            self.conn, name=name, path=str(dest), model=model, owner_id=owner_id
        )
        return db.get_project(self.conn, pid)

    def create(
        self,
        remote_url: str,
        github_token: str | None = None,
        model: str | None = None,
        owner_id: int | None = None,
    ) -> sqlite3.Row:
        """Clone ``remote_url`` into labs/<repo-name> and register it.

        ``github_token`` is this project's own GitHub credential (there is no
        global token): it authenticates the clone and is persisted on the
        project's ``origin`` and row so later push/pull/fetch reuse it. Omit it
        for a public repo. ``model`` is an optional per-project model override
        (one of ``KNOWN_MODEL_IDS``); omit it to use the lab default. The name is
        derived from the repo (``…/foo.git`` -> ``foo``); a collision gets a
        ``_2`` / ``_3`` / … suffix.
        """
        remote_url = _strip_credentials(remote_url.strip())
        if not remote_url:
            raise ProjectError("a git URL is required")
        token = (github_token or "").strip() or None
        model = (model or "").strip() or None
        if model is not None and model not in KNOWN_MODEL_IDS:
            raise ProjectError(f"unknown model: {model}")
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
            self.conn,
            name=name,
            path=str(dest),
            remote_url=remote_url,
            github_token=token,
            model=model,
            owner_id=owner_id,
        )
        return db.get_project(self.conn, pid)

    async def set_model(self, project_id: int, model: str | None) -> dict:
        """Set (or clear, with empty ``model``) a project's model override.

        Persists the choice on the project row and drops the cached session so
        the next turn rebuilds it with the new model. The resumed conversation,
        branch, and working tree are untouched — only the model changes, taking
        effect on the next message (the lab's equivalent of the CLI's ``/model``).
        """
        row = db.get_project(self.conn, project_id)
        if row is None:
            raise ProjectError(f"no such project: {project_id}")
        model = (model or "").strip() or None
        if model is not None and model not in KNOWN_MODEL_IDS:
            raise ProjectError(f"unknown model: {model}")
        async with self.lock(project_id):
            db.set_project_model(self.conn, project_id, model)
            self._sessions.pop(project_id, None)
        return {"model": model or self.config.model}

    async def set_agent_prompt(self, project_id: int, prompt: str) -> dict:
        """Set (empty = clear) the project's extra agent system prompt.

        Drops the cached session like a model switch: the conversation resumes,
        the next turn rebuilds its options with the new prompt.
        """
        if db.get_project(self.conn, project_id) is None:
            raise ProjectError(f"no such project: {project_id}")
        value = prompt.strip() or None
        db.set_project_agent_prompt(self.conn, project_id, value)
        self._sessions.pop(project_id, None)
        return {"agent_prompt": value or ""}

    async def set_mcp_servers(self, project_id: int, raw: str) -> dict:
        """Set (empty = clear) the project's MCP servers (JSON name -> config).

        Validated here so a typo is a 400 at save time, not a broken next turn;
        the SDK gets the parsed dict verbatim. Drops the cached session.
        """
        if db.get_project(self.conn, project_id) is None:
            raise ProjectError(f"no such project: {project_id}")
        value = raw.strip() or None
        if value is not None:
            try:
                parsed = json.loads(value)
            except ValueError as exc:
                raise ProjectError(f"mcp servers must be valid JSON: {exc}") from exc
            if not isinstance(parsed, dict) or not all(
                isinstance(v, dict) for v in parsed.values()
            ):
                raise ProjectError(
                    'mcp servers must be an object of name -> config, e.g. '
                    '{"docs": {"type": "http", "url": "https://..."}}'
                )
            value = json.dumps(parsed, indent=2)
        db.set_project_mcp_servers(self.conn, project_id, value)
        self._sessions.pop(project_id, None)
        return {"mcp_servers": value or ""}

    # --- Skills: .claude/skills/<name>/SKILL.md in the working tree ---------

    def _skills_dir(self, project_id: int) -> Path:
        return self._workspace(project_id).path / ".claude" / "skills"

    async def list_skills(self, project_id: int) -> list[dict]:
        """Skills committed in the project tree: ``{name, description}`` rows."""
        skills_dir = self._skills_dir(project_id)
        if not skills_dir.is_dir():
            return []
        rows: list[dict] = []
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.is_file():
                _, description = _skill_frontmatter(skill_md.read_text(errors="replace"))
                rows.append({"name": child.name, "description": description})
        return rows

    async def add_skill(self, project_id: int, content: str) -> dict:
        """Install an uploaded SKILL.md: the frontmatter ``name:`` names it.

        Writes ``.claude/skills/<name>/SKILL.md`` and commits straight away for
        the same reason uploads are: a dirty tree blocks the next chat session.
        The agent picks the skill up on its next turn (``skills="all"``).
        """
        name, _ = _skill_frontmatter(content)
        if name is None:
            raise ProjectError(
                'SKILL.md needs a frontmatter block with a "name:" field'
            )
        if not _NAME_RE.match(name):
            raise ProjectError(
                f"skill name {name!r} must be letters/digits/._- "
                "(start with a letter or digit)"
            )
        ws = self._workspace(project_id)
        async with self.lock(project_id):
            ws.ensure_repo()
            target = self._skills_dir(project_id) / name
            target.mkdir(parents=True, exist_ok=True)
            (target / "SKILL.md").write_text(content)
            commit = ws.commit_all(f"Add skill {name} via web console") if ws.is_dirty() else None
        self._sessions.pop(project_id, None)
        return {"name": name, "commit": commit}

    async def remove_skill(self, project_id: int, name: str) -> dict:
        """Delete a skill directory from the tree and commit the removal."""
        if not _NAME_RE.match(name or ""):
            raise ProjectError(f"no such skill: {name!r}")
        ws = self._workspace(project_id)
        async with self.lock(project_id):
            ws.ensure_repo()
            target = self._skills_dir(project_id) / name
            if not (target / "SKILL.md").is_file():
                raise ProjectError(f"no such skill: {name!r}")
            shutil.rmtree(target)
            commit = None
            if ws.is_dirty():
                commit = ws.commit_all(f"Remove skill {name} via web console")
        self._sessions.pop(project_id, None)
        return {"name": name, "commit": commit}

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
        # Per-project model override, falling back to the lab default. Reuse the
        # shared config when they match so we don't clone it needlessly.
        model = self.effective_model(row)
        config = self.config if model == self.config.model else replace(self.config, model=model)
        session = LabSession(
            repo_path=row["path"],
            config=config,
            branch=row["branch"] or None,
            base_branch=row["base_branch"] or None,
            session_id=row["last_session_id"],
            branch_started=bool(row["branch"]),
            run_task=self._run_task,
            client_registry=self._client_registry,
            system_append=row["agent_prompt"],
            mcp_servers=_parse_mcp_servers(row["mcp_servers"]),
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

    async def rebase_onto_base(self, project_id: int) -> dict:
        """Rebase a project's chat branch onto its base branch (locally).

        Replaces the earlier merge-base-into-branch action (2026-06-10): the
        session's commits are replayed on top of base's latest, keeping the
        chat branch linear instead of accumulating merge knots. On conflict
        the rebase is aborted (the branch is untouched) and the conflicted
        paths are returned with ``status: "conflicts"`` — the UI offers to
        hand resolution to the project's agent in chat, which can redo the
        rebase and resolve the conflicts itself. The working tree is left on
        the chat branch either way.
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
                raise ProjectError("the chat branch is the base branch; nothing to rebase")
            try:
                commit = ws.rebase(branch, onto=base)
            except WorkspaceConflict as exc:
                return {
                    "status": "conflicts", "base": base, "branch": branch,
                    "files": exc.files,
                }
        return {"status": "ok", "base": base, "branch": branch, "commit": commit}

    async def remove(self, project_id: int) -> dict:
        """Remove a project from the lab — clone, chat history, client mirrors.

        The remote repository is untouched; this only forgets the lab's copy.
        Every *connected* platform client is asked to clean its mirror of the
        project (idempotent — clients without one report ok); a client that is
        offline right now keeps its mirror until it is cleaned another way.
        The clone directory is deleted only if it really lives under labs_dir.
        """
        row = db.get_project(self.conn, project_id)
        if row is None:
            raise ProjectError(f"no such project: {project_id}")
        path = Path(row["path"]).resolve()
        async with self.lock(project_id):
            self._sessions.pop(project_id, None)
            mirrors_cleaned: list[str] = []
            mirror_errors: dict[str, str] = {}
            if self._client_registry is not None:
                for c in self._client_registry.list():
                    try:
                        result = await self._client_registry.clean(c["name"], project=path.name)
                    except ClientError as exc:
                        mirror_errors[c["name"]] = str(exc)
                        continue
                    if result.get("ok"):
                        mirrors_cleaned.append(c["name"])
                    else:
                        mirror_errors[c["name"]] = result.get("error") or "clean failed"
            labs = self.labs_dir.resolve()
            if path != labs and path.is_relative_to(labs) and path.is_dir():
                await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
            db.delete_project(self.conn, project_id)
        self._locks.pop(project_id, None)
        return {
            "name": row["name"],
            "mirrors_cleaned": sorted(mirrors_cleaned),
            "mirror_errors": mirror_errors,
        }

    async def upload_files(
        self, project_id: int, files: list[tuple[str, bytes]], dest: str = ""
    ) -> dict:
        """Write uploaded files into a project's working tree and commit them.

        ``dest`` is a repo-relative directory ("" = root). The commit keeps the
        no-uncommitted-changes invariant a new chat session insists on — an
        upload left dangling would block the next conversation. Traversal and
        ``.git`` targets are refused per file, not for the whole batch.
        """
        from platform_client import manifest

        ws = self._workspace(project_id)
        async with self.lock(project_id):
            ws.ensure_repo()
            written: list[str] = []
            errors: dict[str, str] = {}
            for name, data in files:
                rel = f"{dest.strip('/')}/{Path(name).name}" if dest.strip("/") else Path(name).name
                if ".git" in PurePosixPath(rel).parts:
                    errors[rel] = "refused: targets .git"
                    continue
                try:
                    manifest.write_file(ws.path, rel, data)
                except manifest.PathOutsideRoot:
                    errors[rel] = "path escapes the project root"
                    continue
                written.append(rel)
            commit = None
            if written and ws.is_dirty():
                what = written[0] if len(written) == 1 else f"{len(written)} files"
                commit = ws.commit_all(f"Upload {what} via web console")
        return {"written": sorted(written), "errors": errors, "commit": commit}

    async def delete_path(self, project_id: int, relpath: str) -> dict:
        """Delete a file or directory from a project's working tree and commit.

        Repo-relative; refuses the root, ``.git``, and traversal. A directory
        is removed recursively. The commit keeps the no-uncommitted-changes
        invariant a chat session needs; deleting an untracked/ignored path
        (e.g. ``.lab-uploads/…``) leaves the tree clean, so ``commit`` is None.
        """
        rel = (relpath or "").strip().strip("/")
        if not rel:
            raise ProjectError("no path to delete")
        if ".git" in PurePosixPath(rel).parts or ".." in PurePosixPath(rel).parts:
            raise ProjectError(f"refused: {relpath!r}")
        ws = self._workspace(project_id)
        async with self.lock(project_id):
            ws.ensure_repo()
            root = ws.path.resolve()
            target = (root / rel).resolve()
            if root not in target.parents:
                raise ProjectError("path escapes the project root")
            if not target.exists():
                raise ProjectError(f"no such path: {relpath!r}")
            is_dir = target.is_dir()
            if is_dir:
                shutil.rmtree(target)
            else:
                target.unlink()
            commit = ws.commit_all(f"Delete {rel} via web console") if ws.is_dirty() else None
        return {"deleted": rel, "is_dir": is_dir, "commit": commit}

    async def chat_uploads(self, project_id: int, files: list[tuple[str, bytes]]) -> list[dict]:
        """Save chat attachments under ``.lab-uploads/`` in the working tree.

        Scratch space for "look at this" files: inside the clone so the agent
        can read them with a relative path, but excluded from commits via
        ``.git/info/exclude`` (local-only, never touches tracked files) and
        from client mirrors via the manifest DEFAULT_IGNORES. A random prefix
        keeps repeat uploads of the same filename from colliding.
        """
        ws = self._workspace(project_id)
        async with self.lock(project_id):
            ws.ensure_repo()
            exclude = ws.path / ".git" / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            current = exclude.read_text() if exclude.exists() else ""
            if "/.lab-uploads/" not in current:
                exclude.write_text(current.rstrip("\n") + "\n/.lab-uploads/\n")
            updir = ws.path / ".lab-uploads"
            updir.mkdir(exist_ok=True)
            saved: list[dict] = []
            for name, data in files:
                safe = Path(name).name or "file"
                target = updir / f"{uuid.uuid4().hex[:8]}-{safe}"
                target.write_bytes(data)
                saved.append({"name": safe, "path": f".lab-uploads/{target.name}"})
        return saved

    async def reset_working_tree(self, project_id: int) -> dict:
        """Discard uncommitted changes in a project's working tree.

        Hard reset + clean of untracked files (ignored files survive) on
        whatever branch is checked out — the repair action for a tree left
        dirty by a crashed run or stray artifacts. Commits are never touched.
        """
        ws = self._workspace(project_id)
        async with self.lock(project_id):
            ws.ensure_repo()
            branch = ws.current_branch()
            commit = ws.reset_hard()
        return {"branch": branch, "commit": commit}

    async def archive(self, project_id: int) -> tuple[str, bytes]:
        """Zip a project's working tree for download.

        Contains what the file browser shows: the checked-out tree minus the
        sync ignores (``.git``, ``.venv``, ``__pycache__``, …) — reusing the
        manifest walk so "what syncs" and "what downloads" stay one notion.
        """
        from platform_client import manifest

        ws = self._workspace(project_id)
        async with self.lock(project_id):
            ws.ensure_repo()
            branch = ws.current_branch()

            def _zip() -> bytes:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for rel in sorted(manifest.scan(ws.path)):
                        zf.write(ws.path / rel, rel)
                return buf.getvalue()

            data = await asyncio.to_thread(_zip)
        name = f"{ws.path.name}-{branch.replace('/', '-')}.zip"
        return name, data

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

    def project_root(self, project_id: int) -> Path:
        """A project's working-tree path; its name keys client mirrors."""
        return self._workspace(project_id).path

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
        the next turn starts from a clean context. Chat attachments
        (``.lab-uploads/``) go with the conversation they belonged to — they
        are referenced by messages, never committed, and nothing else manages
        their lifecycle. The branch and the rest of the working tree are left
        untouched.
        """
        row = db.get_project(self.conn, project_id)
        if row is None:
            raise ProjectError(f"no such project: {project_id}")
        async with self.lock(project_id):
            db.clear_messages(self.conn, project_id)
            db.clear_session(self.conn, project_id)
            shutil.rmtree(Path(row["path"]) / ".lab-uploads", ignore_errors=True)
            # Drop the cached session so open() rebuilds it from the now-reset
            # row (no last_session_id) instead of resuming the old context.
            self._sessions.pop(project_id, None)
        return {"cleared": True}

    async def run_turn(self, project_id: int, message: str, *, on_event=None) -> TurnResult:
        """Run one chat turn for a project: persist message, run, persist state."""
        session = self.open(project_id)
        db.record_message(self.conn, project_id=project_id, role="user", content=message)

        async def persisting_event(event: dict) -> None:
            # Persist tool calls and their output as ordered transcript rows so
            # they survive a reload. Autoincrement ids interleave them between
            # the user row above and the final assistant summary below, matching
            # the order they streamed in. Text blocks still aren't persisted
            # per-chunk; the aggregated summary is recorded after the turn.
            kind = event.get("type")
            if kind == "tool_use":
                db.record_message(
                    self.conn, project_id=project_id, role="assistant",
                    content=str(event.get("name") or "tool"), kind="tool_use",
                    meta=json.dumps({"id": event.get("id"), "input": event.get("input")}),
                )
            elif kind == "tool_result":
                db.record_message(
                    self.conn, project_id=project_id, role="assistant",
                    content="", kind="tool_result",
                    meta=json.dumps({
                        "tool_use_id": event.get("tool_use_id"),
                        "content": event.get("content"),
                        "is_error": event.get("is_error"),
                    }),
                )
            if on_event is not None:
                await on_event(event)

        async with self.lock(project_id):
            result = await session.run_turn(message, on_event=persisting_event)
        db.update_project(
            self.conn, project_id, branch=session.branch, last_session_id=session.session_id
        )
        db.record_message(
            self.conn, project_id=project_id, role="assistant", content=result.agent.summary
        )
        return result
