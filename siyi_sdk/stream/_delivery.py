"""Bounded latest-frame delivery across native worker threads."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator

from .models import StreamFrame


class LatestFrames:
    """One replaceable image slot and at most one scheduled loop notification."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._event = asyncio.Event()
        self._lock = threading.Lock()
        self._latest: StreamFrame | None = None
        self._available = False
        self._scheduled = False
        self._closed = False
        self._error: Exception | None = None

    def _schedule(self) -> None:
        # Called with the lock held; callbacks never capture an image reference.
        if not self._scheduled:
            self._scheduled = True
            try:
                self._loop.call_soon_threadsafe(self._notify)
            except RuntimeError:
                self._scheduled = False

    def _notify(self) -> None:
        with self._lock:
            self._scheduled = False
        self._event.set()

    def publish(self, frame: StreamFrame) -> None:
        with self._lock:
            if self._closed:
                return
            self._latest = frame
            self._available = True
            self._schedule()

    def close(self, error: Exception | None = None) -> None:
        with self._lock:
            self._closed = True
            self._error = error
            self._schedule()

    def latest(self) -> StreamFrame | None:
        with self._lock:
            return self._latest

    async def frames(self) -> AsyncGenerator[StreamFrame, None]:
        while True:
            await self._event.wait()
            with self._lock:
                self._event.clear()
                frame = self._latest if self._available else None
                self._available = False
                closed, error = self._closed, self._error
            if frame is not None:
                yield frame
            if closed:
                if error is not None:
                    raise error
                return
