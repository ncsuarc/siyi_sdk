"""Persistent firmware tracking link, connected the way the SIYI AI module is.

The AI module stays connected to the camera's port 37256, keeps the
connection alive, and tracks on the camera's own 1280x720 H.265 stream (0x90),
which arrives with far less delay than RTSP. That stream only becomes
decodable at a keyframe (about every 2.5 s), so the link stays up for as long
as firmware steering is in use, and each lock only switches AI mode.

TCP delivers that stream in bursts (many frames at once, then nothing for a few
hundred ms), so a frame's arrival time says little about when it was captured.
Each frame carries a counter, though, and the camera numbers frames at a steady
rate. :class:`CaptureClock` turns the counter into a capture time.
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

from siyi_sdk.firmware_tracking import CANCEL_SETTLE, FirmwareTrackingClient, Trace

logger = logging.getLogger(__name__)

# Decoded frame (BGR, 1280x720) and its estimated monotonic capture time (see CaptureClock).
FrameSink = Callable[[np.ndarray, float], Awaitable[None]]

RETRY_DELAY = 2.0
VIDEO_STALL = 3.0  # reconnect when no video packet arrives for this long
VIDEO_FPS = 25.0  # the camera's stream; its frame counter measured 25.013 per second
CLOCK_WINDOW = 3.0  # seconds of arrivals CaptureClock looks back over


class CaptureClock:
    """Estimate when each video frame was captured from its frame counter.

    The counter gives the spacing between frames exactly; only the offset between the
    camera's clock and ours is unknown. A frame's ``arrival - index / fps`` is that offset
    plus however long the frame sat in the pipe, so the smallest value over a short window
    is the offset: the delay of the least-held frame, which is what arrival time meant
    before bursts. The window is short enough to follow clock drift (about 6 ms over 40 s
    measured). Estimates never go backwards, so a lock's "no newer frame" check holds.
    """

    def __init__(self, fps: float = VIDEO_FPS, window: float = CLOCK_WINDOW) -> None:
        """Start with no history; ``fps`` is the camera's nominal frame rate."""
        self._fps = fps
        self._window = window
        self._max_jump = 5 * fps  # a counter that skips further than this has restarted
        self._offsets: deque[tuple[float, float]] = deque()  # (arrival, offset), offsets rising
        self._index: int | None = None
        self._capture = 0.0

    def update(self, index: int, arrival: float) -> float:
        """Return the estimated capture time of frame ``index``, which arrived at ``arrival``."""
        if self._index is not None and not 0 <= index - self._index <= self._max_jump:
            self._offsets.clear()  # the counter restarted (a new stream); old offsets don't apply
        self._index = index
        offset = arrival - index / self._fps
        while self._offsets and self._offsets[-1][1] >= offset:
            self._offsets.pop()
        self._offsets.append((arrival, offset))
        while self._offsets[0][0] < arrival - self._window:
            self._offsets.popleft()
        self._capture = max(index / self._fps + self._offsets[0][1], self._capture + 1e-6)
        return self._capture


class _VideoStats:
    """What the video did in the last second: how bursty, how late, how busy the decoder was."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.packets = self.nbytes = self.bursts = self.gaps_over_200 = self.deliveries = 0
        self.max_gap = self.max_decode = self.max_lag = 0.0
        self.max_batch = 0
        self._last = 0.0

    def packet(self, size: int, arrival: float) -> None:
        gap = (arrival - self._last) * 1000 if self._last else 0.0
        self._last = arrival
        self.packets += 1
        self.nbytes += size
        if gap > 60:  # the same split the analysis used: a new burst starts after a 60 ms gap
            self.bursts += 1
        self.gaps_over_200 += gap > 200
        self.max_gap = max(self.max_gap, gap)

    def delivery(self, decode_ms: float, lag_ms: float, batch: int) -> None:
        self.deliveries += 1
        self.max_decode = max(self.max_decode, decode_ms)
        self.max_lag = max(self.max_lag, lag_ms)
        self.max_batch = max(self.max_batch, batch)

    def summary(self, now: float) -> dict[str, Any]:
        return {
            "seconds": round(now - self.started, 2), "packets": self.packets,
            "kbytes": round(self.nbytes / 1024, 1), "bursts": self.bursts,
            "max_gap_ms": round(self.max_gap, 1), "gaps_over_200ms": self.gaps_over_200,
            "pictures": self.deliveries, "max_batch": self.max_batch,
            "max_decode_ms": round(self.max_decode, 1), "max_lag_ms": round(self.max_lag, 1),
        }


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
        # (frame counter, H.265 access unit, arrival time, estimated capture time)
        self._packets: deque[tuple[int, bytes, float, float]] = deque()
        self._clock = CaptureClock()
        self._stats = _VideoStats()
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

    def _report_stats(self) -> None:
        """Trace a once-a-second summary of the video, so a bad second shows up at a glance."""
        now = time.monotonic()
        if now - self._stats.started >= 1.0:
            self._note("video_stats", **self._stats.summary(now))
            self._stats = _VideoStats()

    def _note(self, event: str, **fields: Any) -> None:
        if self._trace is not None:
            with contextlib.suppress(Exception):
                self._trace(event, fields)

    def _queue(self, index: int, data: bytes, arrival: float) -> None:
        self._last_packet = arrival
        self._stats.packet(len(data), arrival)
        self._packets.append((index, data, arrival, self._clock.update(index, arrival)))
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
                self._clock = CaptureClock()
                self._stats = _VideoStats()
                self.last_frame_time = 0.0  # not ready until this connection delivers video
                client = FirmwareTrackingClient(self.ip, trace=self._trace, on_video=self._queue)
                await client.connect()
                self.client = client
                # The stream flag survives disconnects; stop any leftover stream so the
                # mode replies below are not queued behind video.
                await client.stop_video()
                await asyncio.sleep(0.3)
                if await client.get_mode():
                    self._note("ai_mode_was_left_on")
                    # Whatever target the gimbal still holds is dropped the way the AI module
                    # drops it: a "canceled" target, then AI mode off.
                    with contextlib.suppress(Exception):
                        await client.cancel_target()
                        await asyncio.sleep(CANCEL_SETTLE)
                await client.set_mode(False)
                await client.start_video()
                self._last_packet = time.monotonic()
                self.error = None
                decode = asyncio.create_task(self._decode_loop())
                while client.is_connected and not decode.done():
                    await asyncio.sleep(0.2)
                    self._report_stats()
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
            self._stats.delivery((time.monotonic() - started) * 1000,
                                 (batch[-1][2] - batch[-1][3]) * 1000, len(batch))
            # Per delivery: how many packets were waiting, decode time, and packet age.
            # lag_ms: how much longer the newest frame sat in the pipe than the window's best.
            self._note("video_delivered", packets=len(batch), first=batch[0][0], last=batch[-1][0],
                       decode_ms=round((time.monotonic() - started) * 1000, 1),
                       age_ms=round((time.monotonic() - batch[-1][2]) * 1000, 1),
                       lag_ms=round((batch[-1][2] - batch[-1][3]) * 1000, 1),
                       picture=image is not None)
            if image is not None:
                self.last_frame_time = time.monotonic()
                await self._on_frame(image, batch[-1][3])

    def _decode(self, batch: list[tuple[int, bytes, float, float]]) -> np.ndarray | None:
        import av

        latest = None
        for _, data, _, _ in batch:
            try:
                for frame in self._decoder.decode(av.Packet(data)):
                    latest = frame
            except av.error.FFmpegError:
                continue  # before the first keyframe, or a damaged frame
        return None if latest is None else latest.to_ndarray(format="bgr24")
