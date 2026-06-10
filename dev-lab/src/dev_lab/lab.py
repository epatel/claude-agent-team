"""Orchestrate a single lab run: branch -> agent edits -> one commit.

This is the M1 core (see cards/dev-lab.md). ``run_once`` takes ``run_task`` as a
parameter so the orchestration can be tested without invoking the real agent.
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
class RunResult:
    branch: str
    base_sha: str
    commit_sha: str | None
    committed: bool
    agent: AgentResult


def _slugify(text: str, *, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "task"


async def run_once(
    instruction: str,
    *,
    repo_path: str | Path,
    config: Config,
    commit: bool = True,
    branch_prefix: str = "lab/",
    on_event: Callable[[dict], Awaitable[None]] | None = None,
    run_task: RunTask = _run_task,
) -> RunResult:
    """Run one instruction end to end in a local clone.

    Creates a work branch off a clean tree, lets the agent edit files, then
    (optionally) commits everything as a single commit. Returns what happened.
    """
    ws = Workspace(Path(repo_path))
    ws.ensure_repo()
    if ws.is_dirty():
        raise RuntimeError(
            f"workspace {ws.path} has uncommitted changes; refusing to start on a dirty tree"
        )

    base_sha = ws.head_sha()
    branch = f"{branch_prefix}{int(time.time())}-{_slugify(instruction)}"
    ws.create_branch(branch)

    extra: dict = {}
    if on_event is not None:
        extra["on_event"] = on_event
    agent_result = await run_task(instruction, cwd=ws.path, model=config.model, **extra)

    commit_sha: str | None = None
    committed = False
    if commit and ws.is_dirty():
        subject = instruction.strip().splitlines()[0][:72]
        commit_sha = ws.commit_all(f"{subject}\n\n(lab autonomous change)")
        committed = True

    return RunResult(
        branch=branch,
        base_sha=base_sha,
        commit_sha=commit_sha,
        committed=committed,
        agent=agent_result,
    )
