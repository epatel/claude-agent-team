"""Interactive chat session: a continuing conversation on one branch.

Unlike a queued job (independent, fresh branch, no memory), a session keeps a
single working branch and resumes the agent's conversation across turns, so
follow-up messages build on prior work. Turns run sequentially.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from .agent import AgentResult
from .agent import run_task as _run_task
from .config import Config
from .workspace import Workspace

RunTask = Callable[..., Awaitable[AgentResult]]


@dataclass(frozen=True)
class TurnResult:
    branch: str
    commit_sha: str | None
    committed: bool
    agent: AgentResult


def _slugify(text: str, *, max_len: int = 32) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "chat"


class LabSession:
    """One conversational branch over the lab's working clone."""

    def __init__(
        self,
        *,
        repo_path: str | Path,
        config: Config,
        branch: str | None = None,
        base_branch: str | None = None,
        session_id: str | None = None,
        branch_started: bool = False,
        run_task: RunTask = _run_task,
        client_registry=None,
    ) -> None:
        self.workspace = Workspace(Path(repo_path))
        self.config = config
        self.branch = branch or f"chat/{int(time.time())}"
        # Base the new ``chat/<ts>`` branch off this branch on the first turn
        # (``None`` keeps the old behaviour of cutting from current HEAD).
        self.base_branch = base_branch
        self._run_task = run_task
        # Connected platform clients (cards/extension-clients.md) — exposed to
        # the agent as the mcp__lab toolset when present.
        self._client_registry = client_registry
        # ``branch_started`` means the branch already exists (restored across a
        # restart) — check it out rather than create it on the first turn.
        self._started = branch_started
        self.session_id = session_id

    async def run_turn(
        self,
        message: str,
        *,
        on_event: Callable[[dict], Awaitable[None]] | None = None,
    ) -> TurnResult:
        """Run one message: ensure the branch, run the agent (resumed), commit."""
        self.workspace.ensure_repo()
        if not self._started:
            if self.workspace.is_dirty():
                raise RuntimeError(
                    f"workspace {self.workspace.path} has uncommitted changes; "
                    "commit or stash them before starting a chat session"
                )
            self.workspace.create_branch(self.branch, base=self.base_branch)
            self._started = True
        else:
            self.workspace.checkout(self.branch)

        extra: dict = {}
        if on_event is not None:
            extra["on_event"] = on_event
        if self._client_registry is not None:
            extra["client_registry"] = self._client_registry

        result = await self._run_task(
            message,
            cwd=self.workspace.path,
            model=self.config.model,
            resume=self.session_id,
            **extra,
        )
        self.session_id = result.session_id or self.session_id

        commit_sha: str | None = None
        committed = False
        if self.workspace.is_dirty():
            subject = message.strip().splitlines()[0][:72] if message.strip() else "chat turn"
            commit_sha = self.workspace.commit_all(f"{subject}\n\n(lab chat: {self.branch})")
            committed = True

        return TurnResult(
            branch=self.branch, commit_sha=commit_sha, committed=committed, agent=result
        )
