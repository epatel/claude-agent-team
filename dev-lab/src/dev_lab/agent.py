"""Wraps the Claude Agent SDK loop for a single task (see cards/dev-lab.md).

Authenticates via the host's ``claude`` login (Claude subscription) — see
cards/subscription-auth.md. The agent edits files in ``cwd``; the lab owns
commits, so the agent is told not to touch git.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

# File + search + shell, scoped to the workspace via ``cwd``.
DEFAULT_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]

# Appended to Claude Code's built-in preset so the engine still injects the
# working-directory / environment context (a bare custom prompt drops it, and the
# agent then writes files outside the workspace).
_SYSTEM_APPEND = (
    "You are an autonomous software developer working inside a git repository. "
    "Carry out the user's instruction by editing files in the current working "
    "directory, using paths relative to it — never write outside the working "
    "directory. Keep changes minimal and focused on what was asked. "
    "Do NOT run `git commit`, `git push`, or any git history/remote command — the "
    "lab handles commits and pushes. You may use other shell commands as needed."
)


@dataclass(frozen=True)
class AgentResult:
    summary: str
    num_turns: int
    is_error: bool
    total_cost_usd: float | None


async def run_task(
    instruction: str,
    *,
    cwd: str | Path,
    model: str,
    max_turns: int = 40,
    effort: str = "high",
    on_event: Callable[[dict], Awaitable[None]] | None = None,
) -> AgentResult:
    """Run the agent loop for one instruction in ``cwd``; return a result summary.

    Uses ``bypassPermissions`` so the unattended lab does not block on approval
    prompts; the tool set and ``cwd`` bound what the agent can touch. If
    ``on_event`` is given, each assistant text block is streamed to it as
    ``{"type": "agent_message", "text": ...}``.
    """
    options = ClaudeAgentOptions(
        cwd=str(cwd),
        model=model,
        allowed_tools=DEFAULT_TOOLS,
        permission_mode="bypassPermissions",
        system_prompt={"type": "preset", "preset": "claude_code", "append": _SYSTEM_APPEND},
        max_turns=max_turns,
        effort=effort,
    )

    summary_parts: list[str] = []
    num_turns = 0
    is_error = False
    cost: float | None = None

    async for message in query(prompt=instruction, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    summary_parts.append(block.text)
                    if on_event is not None:
                        await on_event({"type": "agent_message", "text": block.text})
        elif isinstance(message, ResultMessage):
            num_turns = message.num_turns
            is_error = message.is_error
            cost = message.total_cost_usd
            if message.result:
                summary_parts = [message.result]

    return AgentResult(
        summary="\n".join(p for p in summary_parts if p).strip(),
        num_turns=num_turns,
        is_error=is_error,
        total_cost_usd=cost,
    )
