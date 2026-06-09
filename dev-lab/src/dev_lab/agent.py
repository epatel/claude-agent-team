"""Wraps the Claude Agent SDK loop for a single task (see cards/dev-lab.md).

Authenticates via the host's ``claude`` login (Claude subscription) — see
cards/subscription-auth.md. The agent edits files in ``cwd``; the lab owns
commits, so the agent is told not to touch git.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

if TYPE_CHECKING:
    from .clients import ClientRegistry

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
    session_id: str | None = None


def _client_tools(registry: ClientRegistry, project_root: Path) -> list:
    """Lab-local SDK tools that route to connected platform clients.

    This is the agent-facing side of the reversed-connection model
    (cards/extension-clients.md): the agent sees typed MCP tools; the registry
    handles the WebSocket dispatch and manifest sync underneath.
    """
    from .clients import ClientError

    @tool(
        "list_clients",
        "List the platform clients currently connected to the lab — machines that "
        "can run commands (build, test, …) on their platform. Each entry has a "
        "name, a platform, and a capability list.",
        {},
    )
    async def list_clients(args: dict) -> dict:
        return {"content": [{"type": "text", "text": json.dumps(registry.list())}]}

    @tool(
        "run_on_client",
        "Run a shell command on a connected platform client, inside a synced "
        "mirror of this project's current working tree (uncommitted changes "
        "included). Use list_clients first to see who is connected. Returns "
        "ok/returncode/stdout/stderr plus the files the run changed on the "
        "client. Use this to build or test on a platform the lab itself lacks.",
        {"client": str, "command": str},
    )
    async def run_on_client(args: dict) -> dict:
        try:
            result = await registry.run(
                str(args["client"]), project_root=project_root, command=str(args["command"])
            )
        except ClientError as exc:
            return {"content": [{"type": "text", "text": f"error: {exc}"}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    return [list_clients, run_on_client]


def _clients_mcp_server(registry: ClientRegistry, project_root: Path):
    return create_sdk_mcp_server("lab", tools=_client_tools(registry, project_root))


def build_agent_options(
    *,
    cwd: str | Path,
    model: str,
    max_turns: int = 40,
    effort: str = "high",
    extensions: dict[str, str] | None = None,
    client_registry: ClientRegistry | None = None,
    resume: str | None = None,
) -> ClaudeAgentOptions:
    """Build the SDK options, wiring remote capabilities as MCP tools.

    ``client_registry`` (connected platform clients) becomes the in-process
    ``mcp__lab`` toolset (`list_clients` / `run_on_client`, bound to ``cwd`` as
    the sync source). ``extensions`` is the legacy SSE wiring, kept while it
    works. ``resume`` (a prior session id) continues that conversation instead
    of starting fresh — used by interactive chat sessions so follow-ups keep
    context.
    """
    allowed = list(DEFAULT_TOOLS)
    mcp_servers: dict[str, dict] = {}
    for name, url in (extensions or {}).items():
        mcp_servers[name] = {"type": "sse", "url": url}
        allowed.append(f"mcp__{name}")  # allow all tools from this extension server
    if client_registry is not None:
        mcp_servers["lab"] = _clients_mcp_server(client_registry, Path(cwd))
        allowed.append("mcp__lab")

    return ClaudeAgentOptions(
        cwd=str(cwd),
        model=model,
        allowed_tools=allowed,
        permission_mode="bypassPermissions",
        system_prompt={"type": "preset", "preset": "claude_code", "append": _SYSTEM_APPEND},
        max_turns=max_turns,
        effort=effort,
        mcp_servers=mcp_servers,
        resume=resume,
    )


async def run_task(
    instruction: str,
    *,
    cwd: str | Path,
    model: str,
    max_turns: int = 40,
    effort: str = "high",
    extensions: dict[str, str] | None = None,
    client_registry: ClientRegistry | None = None,
    resume: str | None = None,
    on_event: Callable[[dict], Awaitable[None]] | None = None,
) -> AgentResult:
    """Run the agent loop for one instruction in ``cwd``; return a result summary.

    Uses ``bypassPermissions`` so the unattended lab does not block on approval
    prompts; the tool set and ``cwd`` bound what the agent can touch. ``extensions``
    (name -> SSE URL) are attached as MCP tool servers. ``resume`` continues a prior
    session. If ``on_event`` is given, assistant text streams as
    ``{"type": "agent_message", ...}`` and tool calls as ``{"type": "tool_use", ...}``.
    """
    options = build_agent_options(
        cwd=cwd, model=model, max_turns=max_turns, effort=effort,
        extensions=extensions, client_registry=client_registry, resume=resume,
    )

    summary_parts: list[str] = []
    num_turns = 0
    is_error = False
    cost: float | None = None
    session_id: str | None = None

    async for message in query(prompt=instruction, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    summary_parts.append(block.text)
                    if on_event is not None:
                        await on_event({"type": "agent_message", "text": block.text})
                elif isinstance(block, ToolUseBlock) and on_event is not None:
                    await on_event({"type": "tool_use", "name": block.name, "input": block.input})
        elif isinstance(message, ResultMessage):
            num_turns = message.num_turns
            is_error = message.is_error
            cost = message.total_cost_usd
            session_id = message.session_id
            if message.result:
                summary_parts = [message.result]

    return AgentResult(
        summary="\n".join(p for p in summary_parts if p).strip(),
        num_turns=num_turns,
        is_error=is_error,
        total_cost_usd=cost,
        session_id=session_id,
    )
