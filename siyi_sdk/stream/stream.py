# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""SIYIStream — the public async streaming API.

Owns a streaming backend instance, runs the frame loop as an asyncio Task,
and dispatches decoded frames to registered callbacks.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Final

import structlog

from .base import AbstractStreamBackend
from .models import StreamBackend, StreamConfig, StreamFrame, StreamState

_log: Final = structlog.get_logger(__name__)

# Type alias for frame callbacks (sync or async).
FrameCallback = Callable[[StreamFrame], Awaitable[None]] | Callable[[StreamFrame], None]


class SIYIStream:
    """Async RTSP video stream receiver for SIYI cameras.

    Delivers decoded frames to registered callbacks. Handles reconnection
    transparently. Thread-safe for callback registration.

    Example:
        stream = SIYIStream(StreamConfig(rtsp_url="rtsp://192.168.144.25:8554/main.264"))

        @stream.on_frame
        async def handle(frame: StreamFrame) -> None:
            cv2.imshow("live", frame.frame)
            cv2.waitKey(1)

        await stream.start()
        await asyncio.sleep(60)
        await stream.stop()

    Args:
        config: Stream configuration including RTSP URL and backend selection.
    """

    def __init__(self, config: StreamConfig) -> None:
        """Initialise SIYIStream.

        Args:
            config: Stream configuration.
        """
        self._config = config
        self._backend: AbstractStreamBackend | None = None
        self._task: asyncio.Task[None] | None = None
        self._callbacks: list[FrameCallback] = []
        self._last_frame: StreamFrame | None = None
        # Rolling window of frame timestamps for FPS calculation.
        self._frame_times: deque[float] = deque()
        self._state = StreamState.STOPPED
        self.last_error: Exception | None = None
        self._operations = asyncio.Lock()
        self._first_frame: asyncio.Future[None] | None = None

    def _select_backend(self) -> AbstractStreamBackend:
        """Select and instantiate the appropriate streaming backend.

        AUTO probes GStreamer → aiortsp → OpenCV in order of preference.

        Returns:
            Instantiated backend ready for connect().

        Raises:
            ImportError: If the requested backend is unavailable and no fallback exists.
        """
        backend_choice = self._config.backend

        if backend_choice is StreamBackend.GSTREAMER:
            from .gstreamer_backend import GStreamerBackend

            return GStreamerBackend(self._config, codec=self._config.codec)

        if backend_choice is StreamBackend.AIORTSP:
            from .aiortsp_backend import AiortspBackend

            return AiortspBackend(self._config)

        if backend_choice is StreamBackend.OPENCV:
            from .opencv_backend import OpenCVBackend

            return OpenCVBackend(self._config)

        # AUTO: probe in preferred order
        try:
            from .gstreamer_backend import _GST_AVAILABLE, GStreamerBackend

            if _GST_AVAILABLE:
                _log.info("backend_selected", backend="gstreamer")
                return GStreamerBackend(self._config, codec=self._config.codec)
        except ImportError:
            pass

        try:
            from .aiortsp_backend import _AIORTSP_AVAILABLE, AiortspBackend

            if _AIORTSP_AVAILABLE:
                _log.info("backend_selected", backend="aiortsp")
                return AiortspBackend(self._config)
        except ImportError:
            pass

        try:
            from .opencv_backend import _OPENCV_AVAILABLE, OpenCVBackend

            if _OPENCV_AVAILABLE:
                _log.info("backend_selected", backend="opencv")
                return OpenCVBackend(self._config)
        except ImportError:
            pass

        raise ImportError(
            "No streaming backend available. Install one of:\n"
            "  pip install opencv-python            # OpenCV fallback\n"
            "  pip install aiortsp av               # pure-Python async\n"
            "  apt install python3-gi gstreamer1.0-plugins-good  # GStreamer (lowest latency)"
        )

    async def start(self) -> None:
        """Start only after a decoded frame, falling through AUTO candidates."""
        async with self._operations:
            if self.is_running:
                return
            if self._backend is not None:
                await self._backend.disconnect()
                self._backend = None
            self._state = StreamState.STARTING
            self.last_error = None
            self._frame_times.clear()
            self._last_frame = None
            choices = (
                (StreamBackend.GSTREAMER, StreamBackend.AIORTSP, StreamBackend.OPENCV)
                if self._config.backend is StreamBackend.AUTO
                else (self._config.backend,)
            )
            configured = self._config.backend
            try:
                for choice in choices:
                    try:
                        self._config.backend = choice
                        self._backend = self._select_backend()
                        self._first_frame = asyncio.get_running_loop().create_future()
                        await self._backend.connect()
                        self._task = asyncio.create_task(
                            self._frame_loop(), name="siyi-stream-loop"
                        )
                        await asyncio.wait_for(
                            asyncio.shield(self._first_frame), self._config.startup_timeout
                        )
                        return
                    except Exception as exc:
                        self.last_error = exc
                        await self._cleanup()
                        if configured is not StreamBackend.AUTO:
                            raise
                        _log.warning("backend_start_failed", backend=choice.value, error=str(exc))
                raise RuntimeError("No backend produced a decoded frame") from self.last_error
            except BaseException as exc:
                if isinstance(exc, Exception):
                    self.last_error = exc
                self._state = StreamState.FAILED
                await self._cleanup()
                raise
            finally:
                self._config.backend = configured

    async def _cleanup(self) -> None:
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._task = None
        if self._first_frame is not None:
            if not self._first_frame.done():
                self._first_frame.cancel()
            elif not self._first_frame.cancelled():
                self._first_frame.exception()
        if self._backend is not None:
            # Retain ownership if shutdown fails; a live native worker forbids restart.
            await self._backend.disconnect()
            self._backend = None

    async def stop(self) -> None:
        """Stop and release resources; report workers that could not terminate."""
        async with self._operations:
            self._state = StreamState.STOPPING
            try:
                await self._cleanup()
            except Exception as exc:
                self.last_error = exc
                self._state = StreamState.FAILED
                raise
            self._state = StreamState.STOPPED

    @property
    def state(self) -> StreamState:
        """Current stream lifecycle, including backend reconnects."""
        if (
            self._state is StreamState.RUNNING
            and self._backend is not None
            and self._backend.state is StreamState.RECONNECTING
        ):
            return StreamState.RECONNECTING
        return self._state

    def on_frame(self, callback: FrameCallback) -> Callable[[], None]:
        """Register a frame callback. Returns an unsubscribe callable.

        Can be used as a decorator or called directly. Both sync and async
        callbacks are supported.

        Args:
            callback: Function accepting a StreamFrame; may be sync or async.

        Returns:
            Zero-argument callable that removes this callback when called.

        Example:
            @stream.on_frame
            async def handle(frame: StreamFrame) -> None:
                process(frame)

            # Or as a call:
            unsub = stream.on_frame(handle)
            unsub()  # stops delivery to handle
        """
        self._callbacks.append(callback)

        def unsubscribe() -> None:
            self.remove_frame_callback(callback)

        return unsubscribe

    def remove_frame_callback(self, callback: FrameCallback) -> None:
        """Unregister a previously added frame callback.

        Args:
            callback: The callback to remove. No-op if not registered.
        """
        with contextlib.suppress(ValueError):
            self._callbacks.remove(callback)

    @property
    def is_running(self) -> bool:
        """Return True if the stream is active.

        Returns:
            bool: True if start() has been called and stop() has not completed.
        """
        return (
            self._task is not None
            and not self._task.done()
            and self.state in (StreamState.RUNNING, StreamState.RECONNECTING)
        )

    @property
    def fps(self) -> float:
        """Rolling 1-second frame rate.

        Returns:
            Number of frames decoded in the last second.
        """
        now = time.monotonic()
        # Prune timestamps older than 1 second.
        while self._frame_times and now - self._frame_times[0] > 1.0:
            self._frame_times.popleft()
        return float(len(self._frame_times))

    @property
    def last_frame(self) -> StreamFrame | None:
        """Most recently decoded frame, or None if no frame received yet.

        Returns:
            StreamFrame or None.
        """
        return self._last_frame

    def _record_frame(self, frame: StreamFrame) -> None:
        self._last_frame = frame
        now = time.monotonic()
        self._frame_times.append(now)
        while self._frame_times and now - self._frame_times[0] > 1.0:
            self._frame_times.popleft()

    async def _frame_loop(self) -> None:
        backend = self._backend
        if backend is None:
            return
        cancelled = False
        try:
            async for frame in backend.frame_generator():
                self._record_frame(frame)
                self._state = StreamState.RUNNING
                if self._first_frame is not None and not self._first_frame.done():
                    self._first_frame.set_result(None)
                await self._dispatch(frame)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:
            self.last_error = exc
            self._state = StreamState.FAILED
            _log.error("frame_loop_error", error=str(exc))
        finally:
            if self._first_frame is not None and not self._first_frame.done():
                self._first_frame.set_exception(
                    self.last_error or RuntimeError("Producer exited before decoding a frame")
                )
            if self._state is StreamState.RUNNING:
                self._state = StreamState.STOPPED
            if not cancelled:
                try:
                    await backend.disconnect()
                except Exception as exc:
                    self.last_error = exc
                    self._state = StreamState.FAILED

    async def _dispatch(self, frame: StreamFrame) -> None:
        """Call all registered callbacks with the given frame.

        Exceptions in individual callbacks are logged and swallowed so that
        one bad callback never kills the stream loop.

        Args:
            frame: The decoded StreamFrame to dispatch.
        """
        for cb in list(self._callbacks):
            try:
                result = cb(frame)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                _log.error(
                    "callback_exception",
                    callback=getattr(cb, "__name__", repr(cb)),
                    exc=type(exc).__name__,
                    msg=str(exc),
                )
