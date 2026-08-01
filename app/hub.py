from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger("channel_guard.hub")


class Hub:
    """Tiny pub/sub broadcaster used to push live events to WebSocket clients."""

    def __init__(self):
        self._clients: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)

    async def publish(self, payload: dict) -> None:
        message = json.dumps(payload, ensure_ascii=False)
        for q in list(self._clients):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                log.warning("dropping event for slow client")
