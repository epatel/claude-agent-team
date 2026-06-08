"""Thin git wrapper for the lab's local working clone (see cards/repo-sync.md).

All operations shell out to ``git`` in the clone directory. Kept deliberately
small: branch, inspect, and commit. Pushing to GitHub arrives in a later
milestone.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


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

    def create_branch(self, name: str) -> None:
        self._git("checkout", "-b", name)

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

    def is_dirty(self) -> bool:
        return bool(self._git("status", "--porcelain"))

    def commit_all(self, message: str) -> str:
        """Stage everything and commit; return the new HEAD sha."""
        self._git("add", "-A")
        self._git("commit", "-m", message)
        return self.head_sha()
