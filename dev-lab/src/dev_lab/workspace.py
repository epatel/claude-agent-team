"""Thin git wrapper for the lab's local working clone (see cards/repo-sync.md).

All operations shell out to ``git`` in the clone directory. Kept deliberately
small: branch, inspect, commit, and the remote verbs (fetch/pull/push) the
console's repo actions are built on.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


class WorkspaceError(RuntimeError):
    pass


class WorkspaceConflict(WorkspaceError):
    """A merge/rebase hit conflicts (already aborted); ``files`` lists the paths."""

    def __init__(self, message: str, files: list[str]) -> None:
        super().__init__(message)
        self.files = files


@dataclass(frozen=True)
class Workspace:
    path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))

    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise WorkspaceError(f"git {' '.join(args)} failed: {detail}")
        return proc.stdout.strip()

    def ensure_repo(self) -> None:
        if not self.path.is_dir():
            raise WorkspaceError(f"workspace path does not exist: {self.path}")
        if self._git("rev-parse", "--is-inside-work-tree") != "true":
            raise WorkspaceError(f"not a git work tree: {self.path}")

    def head_sha(self) -> str:
        return self._git("rev-parse", "HEAD")

    def current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD")

    def create_branch(self, name: str, base: str | None = None) -> None:
        """Create and check out ``name``.

        When ``base`` is ``None`` the new branch is cut from the current HEAD
        (the original behaviour). Otherwise ``base`` is checked out first — if it
        exists only as a remote-tracking ref (``origin/<base>``) a local tracking
        branch is created from it — and ``name`` is then cut from ``base``.
        """
        if base is not None:
            self._checkout_base(base)
        self._git("checkout", "-b", name)

    def _checkout_base(self, base: str) -> None:
        """Check out ``base``, materialising it from ``origin/<base>`` if needed."""
        if self.branch_exists(base):
            self.checkout(base)
            return
        try:
            self._git("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base}")
        except WorkspaceError:
            raise WorkspaceError(
                f"base branch not found locally or on origin: {base}"
            ) from None
        self._git("checkout", "-b", base, f"origin/{base}")

    def list_branches(self) -> list[str]:
        """Return available branch names, deduped and sorted.

        Includes local heads plus remote-tracking refs under ``origin/`` (with
        the ``origin/`` prefix stripped), so a branch that exists only as
        ``origin/<name>`` can still be targeted by ``<name>``. ``origin/HEAD`` is
        excluded.
        """
        out = self._git(
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads",
            "refs/remotes/origin",
        )
        names: set[str] = set()
        for line in out.splitlines():
            line = line.strip()
            if not line or line.endswith("/HEAD"):
                continue
            if line.startswith("origin/"):
                names.add(line[len("origin/"):])
            else:
                names.add(line)
        return sorted(names)

    def checkout(self, name: str) -> None:
        self._git("checkout", name)

    def branch_exists(self, name: str) -> bool:
        try:
            self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}")
            return True
        except WorkspaceError:
            return False

    def default_branch(self) -> str:
        """Best-effort base branch: origin/HEAD, else main/master, else current."""
        try:
            ref = self._git("rev-parse", "--abbrev-ref", "origin/HEAD")
            if ref.startswith("origin/"):
                return ref.split("/", 1)[1]
        except WorkspaceError:
            pass
        for candidate in ("main", "master"):
            if self.branch_exists(candidate):
                return candidate
        return self.current_branch()

    def merge(self, base: str, branch: str, *, message: str) -> str:
        """Check out ``base`` and merge ``branch`` into it (no-ff).

        Aborts the merge and raises ``WorkspaceError`` on conflict; returns the new
        ``base`` HEAD sha on success.
        """
        self.checkout(base)
        proc = subprocess.run(
            ["git", "merge", "--no-ff", "-m", message, branch],
            cwd=self.path,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            subprocess.run(
                ["git", "merge", "--abort"], cwd=self.path, capture_output=True, text=True
            )
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise WorkspaceError(f"merge of {branch} into {base} failed (conflicts?): {detail}")
        return self.head_sha()

    def rebase(self, branch: str, *, onto: str) -> str:
        """Check out ``branch`` and rebase it onto ``onto`` (linear history).

        On conflict the rebase is aborted (the branch is left exactly as it
        was) and ``WorkspaceConflict`` is raised carrying the conflicted paths,
        so the caller can hand resolution to something smarter — e.g. the
        project's agent. Returns the new ``branch`` HEAD sha on success.
        """
        self.checkout(branch)
        proc = subprocess.run(
            ["git", "rebase", onto], cwd=self.path, capture_output=True, text=True
        )
        if proc.returncode != 0:
            conflicted = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                cwd=self.path, capture_output=True, text=True,
            ).stdout.split()
            subprocess.run(
                ["git", "rebase", "--abort"], cwd=self.path, capture_output=True, text=True
            )
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise WorkspaceConflict(
                f"rebase of {branch} onto {onto} hit conflicts: {detail}", sorted(conflicted)
            )
        return self.head_sha()

    def reset_hard(self) -> str:
        """Discard uncommitted changes: hard-reset to HEAD and clean untracked.

        ``git clean -fd`` removes untracked files and dirs but keeps ignored
        ones (no ``-x``), so local venvs/caches survive. Commits are untouched.
        Returns the HEAD sha.
        """
        self._git("reset", "--hard", "HEAD")
        self._git("clean", "-fd")
        return self.head_sha()

    def pull(self, branch: str) -> str:
        """Fast-forward ``branch`` from origin; return its new HEAD sha.

        Checks out ``branch``, pulls (``--ff-only``), then restores whatever
        branch was checked out before. Raises ``WorkspaceError`` if the pull is
        not a fast-forward (diverged history).
        """
        current = self.current_branch()
        self.checkout(branch)
        proc = subprocess.run(
            ["git", "pull", "--ff-only", "origin", branch],
            cwd=self.path,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            if current != branch:
                self.checkout(current)
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise WorkspaceError(
                f"pull of {branch} from origin failed (not a fast-forward?): {detail}"
            )
        sha = self.head_sha()
        if current != branch:
            self.checkout(current)
        return sha

    def push(self, branch: str) -> str:
        """Push ``branch`` to origin; return its HEAD sha.

        Does not touch the working tree. Raises ``WorkspaceError`` if the push is
        rejected (non-fast-forward remote, missing remote, auth failure, …).
        """
        self._git("push", "origin", branch)
        return self._git("rev-parse", branch)

    def fetch(self, branch: str) -> str:
        """Fetch ``branch`` from origin; return the remote-tracking HEAD sha.

        Updates ``origin/<branch>`` without touching the working tree or the
        checked-out branch. Raises ``WorkspaceError`` if the fetch fails
        (missing remote, auth failure, unknown branch, …).
        """
        self._git("fetch", "origin", branch)
        return self._git("rev-parse", f"origin/{branch}")

    def remote_url(self) -> str | None:
        """The ``origin`` remote URL, or ``None`` if there is no origin."""
        try:
            return self._git("remote", "get-url", "origin")
        except WorkspaceError:
            return None

    def set_remote_url(self, url: str) -> None:
        """Point ``origin`` at ``url`` (used to (re)apply a project's auth token)."""
        self._git("remote", "set-url", "origin", url)

    def is_dirty(self) -> bool:
        return bool(self._git("status", "--porcelain"))

    def uncommitted_count(self) -> int:
        """Number of changed/untracked entries in the working tree."""
        out = self._git("status", "--porcelain")
        return sum(1 for line in out.splitlines() if line.strip())

    def _resolve_compare_ref(self, base: str) -> str | None:
        """The first of ``base`` / ``origin/<base>`` that resolves, else None."""
        for cand in (base, f"origin/{base}"):
            try:
                self._git("rev-parse", "--verify", "--quiet", cand)
                return cand
            except WorkspaceError:
                continue
        return None

    def ahead_behind(self, base: str) -> tuple[int, int] | None:
        """(ahead, behind) commit counts of HEAD vs ``base``.

        ``ahead`` is commits on HEAD not on base (the session's work); ``behind``
        is commits on base not on HEAD (base moved on). Returns ``None`` when the
        base ref can't be resolved (e.g. a fresh repo with no base yet).
        """
        ref = self._resolve_compare_ref(base)
        if ref is None:
            return None
        try:
            out = self._git("rev-list", "--left-right", "--count", f"{ref}...HEAD")
        except WorkspaceError:
            return None
        left, _, right = out.partition("\t")
        try:
            return int(right), int(left)  # ahead (HEAD side), behind (base side)
        except ValueError:
            return None

    def last_commit(self) -> dict | None:
        """The current HEAD commit's short sha, subject, and relative time."""
        try:
            out = self._git("log", "-1", "--format=%H%x1f%s%x1f%cr")
        except WorkspaceError:
            return None  # an empty repo with no commits yet
        parts = out.split("\x1f")
        if len(parts) < 3:
            return None
        return {"sha": parts[0][:10], "subject": parts[1], "relative": parts[2]}

    def commit_all(self, message: str) -> str:
        """Stage everything and commit; return the new HEAD sha."""
        self._git("add", "-A")
        self._git("commit", "-m", message)
        return self.head_sha()

    # --- inspection: commit diffs + a read-only repo browser ----------------

    @staticmethod
    def _require_sha(sha: str) -> None:
        if not _SHA_RE.match(sha or ""):
            raise WorkspaceError(f"invalid commit sha: {sha!r}")

    def commit_subject(self, sha: str) -> str:
        """The one-line subject of a commit."""
        self._require_sha(sha)
        return self._git("show", "-s", "--format=%s", sha)

    def commit_diff(self, sha: str) -> str:
        """Unified patch introduced by a single commit.

        Uses ``git show`` so a root commit (no parent) still diffs cleanly
        against the empty tree. ``--format=`` drops the commit header, leaving
        just the patch the frontend renders.
        """
        self._require_sha(sha)
        return self._git("show", "--no-color", "--format=", sha)

    def _safe_path(self, rel: str) -> Path:
        """Resolve a repo-relative path, refusing anything outside the clone."""
        rel = (rel or "").strip().lstrip("/")
        root = self.path.resolve()
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            raise WorkspaceError(f"path escapes the repo: {rel!r}")
        return target

    def diff_status(self, base: str) -> dict[str, str]:
        """Map each path that differs from ``base`` to its change kind.

        Compares the *working tree* (committed-on-this-branch changes plus
        uncommitted edits) against ``base``, so the repo browser can mark which
        files have diverged and surface files that exist only on ``base``. Keys
        are repo-relative paths; values are ``"new"`` (absent from base),
        ``"modified"`` (content differs), or ``"deleted"`` (in base, absent from
        the working tree). Returns ``{}`` when the tree matches ``base``.
        """
        status: dict[str, str] = {}
        for line in self._git("diff", "--name-status", base).splitlines():
            cols = line.split("\t")
            code = cols[0][:1]
            path = cols[-1]  # for renames git lists the new path last
            if code == "A":
                status[path] = "new"
            elif code == "D":
                status[path] = "deleted"
            else:  # M, T, C, R, …
                status[path] = "modified"
        # Untracked working-tree files aren't in the diff; flag them as new.
        for path in self._git("ls-files", "--others", "--exclude-standard").splitlines():
            status.setdefault(path, "new")
        return status

    def list_tree(self, rel: str = "") -> list[dict]:
        """List one directory level of the working tree (``.git`` excluded).

        Returns ``{name, path, type}`` entries (dirs first, then files), each
        ``path`` repo-relative so the caller can drill down lazily.
        """
        root = self.path.resolve()
        target = self._safe_path(rel)
        if not target.is_dir():
            raise WorkspaceError(f"not a directory: {rel!r}")
        entries: list[dict] = []
        for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if child.name == ".git":
                continue
            entries.append(
                {
                    "name": child.name,
                    "path": str(child.relative_to(root)),
                    "type": "dir" if child.is_dir() else "file",
                }
            )
        return entries

    def file_path(self, rel: str) -> Path:
        """Resolve a repo-relative path to an existing file for raw serving.

        Same guards as :meth:`read_file` (path-traversal refused, ``.git``
        hidden) but returns the absolute path so the caller can stream the bytes
        — used to serve images and other binaries the browser renders directly.
        """
        target = self._safe_path(rel)
        parts = target.relative_to(self.path.resolve()).parts
        if ".git" in parts:
            raise WorkspaceError(f"refused: {rel!r}")
        if not target.is_file():
            raise WorkspaceError(f"not a file: {rel!r}")
        return target

    def read_file(self, rel: str, *, max_bytes: int = 512 * 1024) -> dict:
        """Read a working-tree file for display.

        Flags binary / oversized content instead of returning raw bytes, so the
        browser never tries to render a megabyte of a non-text blob.
        """
        target = self._safe_path(rel)
        if not target.is_file():
            raise WorkspaceError(f"not a file: {rel!r}")
        data = target.read_bytes()
        size = len(data)
        chunk = data[:max_bytes]
        if b"\x00" in chunk:
            return {"path": rel, "binary": True, "size": size, "truncated": False, "content": ""}
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            return {"path": rel, "binary": True, "size": size, "truncated": False, "content": ""}
        return {
            "path": rel,
            "binary": False,
            "size": size,
            "truncated": size > max_bytes,
            "content": text,
        }
