"""Rendering + helpers for the chat client (see cards/chat-client.md)."""

from __future__ import annotations

import json
from typing import Any


def format_event(event: dict[str, Any]) -> str:
    """Render one lab event (received over WebSocket) as a line of text."""
    kind = event.get("type")
    job = event.get("job_id", "")

    if kind == "ack":
        return f"[queued {event.get('job_id')}] {event.get('instruction', '')}"
    if kind == "job_running":
        return f"[running {job}] {event.get('instruction', '')}"
    if kind == "agent_message":
        return event.get("text", "")
    if kind == "job_done":
        sha = event.get("commit_sha")
        return f"[done {job}] branch={event.get('branch')} commit={sha[:12] if sha else 'none'}"
    if kind == "job_failed":
        return f"[failed {job}] {event.get('error', '')}"
    if kind == "error":
        return f"[error] {event.get('error')}"
    return json.dumps(event)
