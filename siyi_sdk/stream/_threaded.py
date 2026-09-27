"""Ownership and responsive shutdown for native video workers."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator

from ._delivery import LatestFrames
from .base import AbstractStreamBackend
from .models import StreamConfig, StreamFrame, StreamState


class ThreadedBackend(AbstractStreamBackend):
    BACKEND_NAME: str

    def __init__(self, config: StreamConfig) -> None:
        super().__init__(config)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._delivery: LatestFrames | None = None
        self._operations = asyncio.Lock()

    async def connect(self) -> None:
        async with self._operations:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("Previous video worker is still alive")
            self._stop_event.clear()
            self.last_error = None
            self.state = StreamState.STARTING
            self._delivery = LatestFrames()
            self._thread = threading.Thread(
                target=self._run, name=f"siyi-{self.BACKEND_NAME}", daemon=True
            )
            self._thread.start()

    async def disconnect(self) -> None:
        async with self._operations:
            self.state = StreamState.STOPPING
            self._stop_event.set()
            if self._delivery:
                self._delivery.close()
            if self._thread is not None:
                await asyncio.to_thread(self._thread.join, 5.0)
                if self._thread.is_alive():
                    self.last_error = RuntimeError("Video worker did not stop within five seconds")
                    self.state = StreamState.FAILED
                    raise self.last_error
                self._thread = None
            self.state = StreamState.STOPPED

    def _run(self) -> None:
        try:
            self._worker()
        except Exception as exc:
            self.last_error = exc
            self.state = StreamState.FAILED
        finally:
            if self.state is not StreamState.FAILED:
                self.state = StreamState.STOPPED
            if self._delivery:
                self._delivery.close(self.last_error)

    def _worker(self) -> None:
        raise NotImplementedError

    def frame_available(self) -> bool:
        return self.read_frame_nowait() is not None

    def read_frame_nowait(self) -> StreamFrame | None:
        return self._delivery.latest() if self._delivery else None

    async def frame_generator(self) -> AsyncGenerator[StreamFrame, None]:
        if self._delivery:
            async for frame in self._delivery.frames():
                yield frame
