"""In-process pub/sub event bus for streaming lab activity to connected clients.

The supervisor publishes job-lifecycle and agent-output events; the WebSocket
control surface subscribes one queue per connected client and forwards them. A
slow client's queue drops events rather than blocking the lab.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

Event = dict[str, Any]


class EventBus:
    def __init__(self, max_queue: int = 1000) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._max_queue = max_queue

    async def publish(self, event: Event) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # drop for a slow consumer rather than stall the lab

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[Event]]:
        q: asyncio.Queue[Event] = asyncio.Queue(self._max_queue)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
