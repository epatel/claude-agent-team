"""Rendering + helpers for the chat client (see cards/chat-client.md)."""

from __future__ import annotations

import json
from typing import Any


def _tool_summary(event: dict[str, Any]) -> str:
    name = event.get("name", "?")
    inp = event.get("input")
    if isinstance(inp, dict):
        hint = inp.get("command") or inp.get("file_path") or inp.get("path") or inp.get("pattern")
        if hint:
            return f"{name}  {str(hint)[:80]}"
    return name


def format_event(event: dict[str, Any]) -> str:
    """Render one lab event (received over WebSocket) as a line of text."""
    kind = event.get("type")
    job = event.get("job_id", "")
    branch = event.get("branch", "")

    # Interactive chat turns
    if kind == "turn_running":
        return f"… running on {branch}"
    if kind == "turn_done":
        sha = event.get("commit_sha")
        return f"✓ {branch}  {('commit ' + sha[:12]) if sha else '(no commit)'}"
    if kind == "turn_failed":
        return f"✗ {branch}: {event.get('error', '')}"

    # Streamed agent activity
    if kind == "agent_message":
        return event.get("text", "")
    if kind == "tool_use":
        return f"  ⤷ {_tool_summary(event)}"

    # Queued jobs (submit)
    if kind == "ack":
        return f"[queued {event.get('job_id')}] {event.get('instruction', '')}"
    if kind == "job_running":
        return f"[running {job}] {event.get('instruction', '')}"
    if kind == "job_done":
        sha = event.get("commit_sha")
        return f"[done {job}] branch={event.get('branch')} commit={sha[:12] if sha else 'none'}"
    if kind == "job_failed":
        return f"[failed {job}] {event.get('error', '')}"

    if kind == "error":
        return f"[error] {event.get('error')}"
    return json.dumps(event)
