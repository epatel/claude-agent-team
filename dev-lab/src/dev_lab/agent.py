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
        "client. By default the sync deletes mirror files not in the project "
        "tree — including the previous run's build artifacts; pass `preserve` "
        "glob patterns to keep artifacts/caches between runs (e.g. "
        "[\"target/*\", \"example/hello\"]). Use this to build or test on a "
        "platform the lab itself lacks.",
        {
            "type": "object",
            "properties": {
                "client": {"type": "string", "description": "client name from list_clients"},
                "command": {"type": "string", "description": "shell command to run in the mirror"},
                "preserve": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "glob patterns of mirror paths to keep across runs "
                                   "(note: * also matches across / )",
                },
            },
            "required": ["client", "command"],
        },
    )
    async def run_on_client(args: dict) -> dict:
        try:
            result = await registry.run(
                str(args["client"]),
                project_root=project_root,
                command=str(args["command"]),
                preserve=[str(p) for p in args.get("preserve") or []],
            )
        except ClientError as exc:
            return {"content": [{"type": "text", "text": f"error: {exc}"}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool(
        "fetch_from_client",
        "Copy files from a platform client's project mirror into this "
        "project's working tree on the lab — how run artifacts (binaries, "
        "reports, logs) get back from the client. Paths are relative to the "
        "project root, exactly as listed in a run's changed-files report. "
        "Fetched files land in the working tree, so they will be committed "
        "with the session unless .gitignored. Returns which paths were "
        "written and any per-path errors.",
        {
            "type": "object",
            "properties": {
                "client": {"type": "string", "description": "client name from list_clients"},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "mirror-relative paths to fetch",
                },
            },
            "required": ["client", "paths"],
        },
    )
    async def fetch_from_client(args: dict) -> dict:
        from platform_client import manifest

        try:
            fetched = await registry.fetch(
                str(args["client"]),
                project=project_root.name,
                paths=[str(p) for p in args.get("paths") or []],
            )
        except ClientError as exc:
            return {"content": [{"type": "text", "text": f"error: {exc}"}], "is_error": True}
        written: list[str] = []
        errors = dict(fetched["errors"])
        for path, data in fetched["files"].items():
            try:
                manifest.write_file(project_root, path, data)
                written.append(path)
            except manifest.PathOutsideRoot:
                errors[path] = "path escapes the project root"
        summary = {"written": sorted(written), "errors": errors}
        return {"content": [{"type": "text", "text": json.dumps(summary)}]}

    return [list_clients, run_on_client, fetch_from_client]


def _clients_mcp_server(registry: ClientRegistry, project_root: Path):
    return create_sdk_mcp_server("lab", tools=_client_tools(registry, project_root))


def build_agent_options(
    *,
    cwd: str | Path,
    model: str,
    max_turns: int = 40,
    effort: str = "high",
    client_registry: ClientRegistry | None = None,
    resume: str | None = None,
    system_append: str | None = None,
    extra_mcp_servers: dict[str, dict] | None = None,
) -> ClaudeAgentOptions:
    """Build the SDK options, wiring remote capabilities as MCP tools.

    ``client_registry`` (connected platform clients) becomes the in-process
    ``mcp__lab`` toolset (`list_clients` / `run_on_client`, bound to ``cwd`` as
    the sync source). ``resume`` (a prior session id) continues that
    conversation instead of starting fresh — used by interactive chat sessions
    so follow-ups keep context. ``system_append`` is the project's own prompt
    (console agent tab), appended after the lab's standing instructions.
    ``extra_mcp_servers`` (name -> SDK config: stdio/sse/http) are the
    project's MCP servers; all their tools are allowed. ``skills="all"`` with
    ``setting_sources=["project"]`` enables exactly the skills committed under
    the project's ``.claude/skills/`` — without pinning the sources the SDK
    defaults to ``["user", "project"]`` and the lab service user's
    ``~/.claude`` (skills, settings) would leak into every project's agent.
    """
    allowed = list(DEFAULT_TOOLS)
    mcp_servers: dict[str, dict] = dict(extra_mcp_servers or {})
    allowed += [f"mcp__{name}" for name in mcp_servers]
    if client_registry is not None:
        mcp_servers["lab"] = _clients_mcp_server(client_registry, Path(cwd))
        allowed.append("mcp__lab")
    append = _SYSTEM_APPEND
    if system_append and system_append.strip():
        append = f"{_SYSTEM_APPEND}\n\n## Project instructions\n\n{system_append.strip()}"

    return ClaudeAgentOptions(
        cwd=str(cwd),
        model=model,
        allowed_tools=allowed,
        permission_mode="bypassPermissions",
        system_prompt={"type": "preset", "preset": "claude_code", "append": append},
        max_turns=max_turns,
        effort=effort,
        mcp_servers=mcp_servers,
        resume=resume,
        skills="all",
        setting_sources=["project"],
    )


async def run_task(
    instruction: str,
    *,
    cwd: str | Path,
    model: str,
    max_turns: int = 40,
    effort: str = "high",
    client_registry: ClientRegistry | None = None,
    resume: str | None = None,
    on_event: Callable[[dict], Awaitable[None]] | None = None,
    system_append: str | None = None,
    extra_mcp_servers: dict[str, dict] | None = None,
) -> AgentResult:
    """Run the agent loop for one instruction in ``cwd``; return a result summary.

    Uses ``bypassPermissions`` so the unattended lab does not block on approval
    prompts; the tool set and ``cwd`` bound what the agent can touch. ``resume``
    continues a prior session. If ``on_event`` is given, assistant text streams
    as ``{"type": "agent_message", ...}`` and tool calls as
    ``{"type": "tool_use", ...}``.
    """
    options = build_agent_options(
        cwd=cwd, model=model, max_turns=max_turns, effort=effort,
        client_registry=client_registry, resume=resume,
        system_append=system_append, extra_mcp_servers=extra_mcp_servers,
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
