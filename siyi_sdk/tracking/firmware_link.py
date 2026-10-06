"""Persistent firmware tracking link, connected the way the SIYI AI module is.

The AI module stays connected to the camera's port 37256, keeps the
connection alive, and tracks on the camera's own 1280x720 H.265 stream (0x90),
which arrives with far less delay than RTSP. That stream only becomes
decodable at a keyframe (about every 2.5 s), so the link stays up for as long
as firmware steering is in use, and each lock only switches AI mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import numpy as np

from siyi_sdk.firmware_tracking import FirmwareTrackingClient, Trace

logger = logging.getLogger(__name__)

# Decoded frame (BGR, 1280x720) and its packet's monotonic arrival time.
FrameSink = Callable[[np.ndarray, float], Awaitable[None]]

RETRY_DELAY = 2.0
VIDEO_STALL = 3.0  # reconnect when no video packet arrives for this long


class FirmwareLink:
    """Keep one port 37256 connection open, stream and decode the camera's video.

    After every (re)connection AI mode is switched off: the camera keeps it on
    across disconnects, and no lock can survive a lost connection.
    """

    def __init__(self, ip: str, *, on_frame: FrameSink, trace: Trace | None = None) -> None:
        """Prepare a link to ``ip``; call :meth:`start` to run it."""
        self.ip = ip
        self._on_frame = on_frame
        self._trace = trace
        self.client: FirmwareTrackingClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._packets: deque[tuple[int, bytes, float]] = deque()
        self._wake = asyncio.Event()
        self._decoder: Any = None
        self._last_packet = 0.0
        self.last_frame_time = 0.0
        self.error: str | None = None

    @property
    def ready(self) -> bool:
        """Whether the link is connected and has delivered a decoded frame recently."""
        client = self.client
        return (
            client is not None
            and client.is_connected
            and time.monotonic() - self.last_frame_time < 1.0
        )

    def start(self) -> None:
        """Run the connect/stream/decode loop in the background."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop streaming, switch AI mode off and disconnect."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._close_client()

    def _note(self, event: str, **fields: Any) -> None:
        if self._trace is not None:
            with contextlib.suppress(Exception):
                self._trace(event, fields)

    def _queue(self, index: int, data: bytes, arrival: float) -> None:
        self._last_packet = arrival
        self._packets.append((index, data, arrival))
        self._wake.set()

    async def _close_client(self) -> None:
        client, self.client = self.client, None
        if client is None:
            return
        if client.is_connected:
            with contextlib.suppress(Exception):
                await client.stop_video()
            with contextlib.suppress(Exception):
                await client.set_mode(False)
        with contextlib.suppress(Exception):
            await client.close()

    async def _run(self) -> None:
        while True:
            decode = None
            try:
                import av  # PyAV decodes the camera's H.265

                self._decoder = av.CodecContext.create("hevc", "r")
                self._decoder.thread_type = "SLICE"  # frame threading would add frames of delay
                self._packets.clear()
                self.last_frame_time = 0.0  # not ready until this connection delivers video
                client = FirmwareTrackingClient(self.ip, trace=self._trace, on_video=self._queue)
                await client.connect()
                self.client = client
                if await client.get_mode():
                    self._note("ai_mode_was_left_on")
                await client.set_mode(False)
                await client.start_video()
                self._last_packet = time.monotonic()
                self.error = None
                decode = asyncio.create_task(self._decode_loop())
                while client.is_connected and not decode.done():
                    await asyncio.sleep(0.2)
                    if time.monotonic() - self._last_packet > VIDEO_STALL:
                        raise TimeoutError(f"No camera video for {VIDEO_STALL:g} s")
                if decode.done():
                    decode.result()
                raise ConnectionError(client.error or "Tracking connection closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = f"Firmware link: {exc}"
                self._note("link_error", error=repr(exc))
                logger.warning(self.error)
            finally:
                if decode is not None:
                    decode.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await decode
            await self._close_client()
            await asyncio.sleep(RETRY_DELAY)

    async def _decode_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            batch = list(self._packets)
            self._packets.clear()
            if not batch:
                continue
            # Decode every packet (later frames reference earlier ones) but deliver
            # only the newest picture, so a burst never builds a backlog.
            started = time.monotonic()
            image = await asyncio.to_thread(self._decode, batch)
            # Per delivery: how many packets were waiting, decode time, and packet age.
            self._note("video_delivered", packets=len(batch), first=batch[0][0], last=batch[-1][0],
                       decode_ms=round((time.monotonic() - started) * 1000, 1),
                       age_ms=round((time.monotonic() - batch[-1][2]) * 1000, 1),
                       picture=image is not None)
            if image is not None:
                self.last_frame_time = time.monotonic()
                await self._on_frame(image, batch[-1][2])

    def _decode(self, batch: list[tuple[int, bytes, float]]) -> np.ndarray | None:
        import av

        latest = None
        for _, data, _ in batch:
            try:
                for frame in self._decoder.decode(av.Packet(data)):
                    latest = frame
            except av.error.FFmpegError:
                continue  # before the first keyframe, or a damaged frame
        return None if latest is None else latest.to_ndarray(format="bgr24")
