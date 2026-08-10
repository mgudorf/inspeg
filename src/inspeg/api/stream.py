"""Server-sent events: live store/context/hud notifications for the HUD.

SSE over WebSocket deliberately (ADR 0007 review): SSE requests pass through
the exact same middleware stack as every other HTTP request — TrustedHost and
the origin check apply unmodified — where a WebSocket's Origin enforcement
would be hand-rolled.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

_QUEUE_LIMIT = 1000
_KEEPALIVE_SECONDS = 15


class EventBus:
    """Bridges store-thread commits onto the asyncio loop's SSE subscribers.

    ``publish_threadsafe`` may be called from any thread (it is a
    ``Store.on_commit`` subscriber); delivery happens on the loop. Events are
    fire-and-forget: a slow subscriber is dropped rather than backing up the
    writer.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def publish_store_events(self, events: list[dict]) -> None:
        if events:
            self.publish_threadsafe({"type": "store", "events": events})

    def publish_threadsafe(self, event: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return  # nobody has ever subscribed; nothing to notify
        loop.call_soon_threadsafe(self._publish, event)

    def _publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            if queue.qsize() >= _QUEUE_LIMIT:
                self._subscribers.discard(queue)
                continue
            queue.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        self._loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)


def stream_router(bus: EventBus) -> APIRouter:
    router = APIRouter()

    @router.get("/api/events/stream")
    async def events_stream(ttl: float | None = None) -> StreamingResponse:
        """SSE stream. ``ttl`` (seconds) bounds the connection's lifetime —
        EventSource clients auto-reconnect, and a finite stream is also what
        makes this endpoint testable without an early-close deadlock."""

        async def generate():
            queue = bus.subscribe()
            deadline = None if ttl is None else asyncio.get_running_loop().time() + ttl
            try:
                yield ": connected\n\n"
                while True:
                    wait = _KEEPALIVE_SECONDS
                    if deadline is not None:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            return
                        wait = min(wait, remaining)
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=wait)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return router
