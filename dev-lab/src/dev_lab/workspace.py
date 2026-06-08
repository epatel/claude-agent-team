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

    def is_dirty(self) -> bool:
        return bool(self._git("status", "--porcelain"))

    def commit_all(self, message: str) -> str:
        """Stage everything and commit; return the new HEAD sha."""
        self._git("add", "-A")
        self._git("commit", "-m", message)
        return self.head_sha()
