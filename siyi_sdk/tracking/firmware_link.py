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
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from fractions import Fraction
from typing import Any, Literal

import numpy as np

from siyi_sdk.firmware_tracking import CANCEL_SETTLE, FirmwareTrackingClient, Trace

logger = logging.getLogger(__name__)

# Full-resolution decoded frame and its estimated monotonic capture time (see CaptureClock).
FrameSink = Callable[[np.ndarray[Any, Any], float], Awaitable[None]]

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
            "seconds": round(now - self.started, 2),
            "packets": self.packets,
            "kbytes": round(self.nbytes / 1024, 1),
            "bursts": self.bursts,
            "max_gap_ms": round(self.max_gap, 1),
            "gaps_over_200ms": self.gaps_over_200,
            "pictures": self.deliveries,
            "max_batch": self.max_batch,
            "max_decode_ms": round(self.max_decode, 1),
            "max_lag_ms": round(self.max_lag, 1),
        }


Packet = tuple[int, bytes, float, float]
Picture = tuple[np.ndarray[Any, Any], float, float]
FailureSink = Callable[[str], Awaitable[None]]


class _Decoder:
    """One session's native codec and bounded packet timestamp association."""

    def __init__(self, image_format: str, thread_count: int, stamp_limit: int) -> None:
        import av

        self.codec = av.CodecContext.create("hevc", "r")
        self.codec.thread_type = "SLICE"
        self.codec.thread_count = thread_count
        self.image_format = image_format
        self.stamp_limit = stamp_limit
        self.stamps: dict[int, tuple[float, float]] = {}
        self.sequence = 0

    def decode(self, batch: list[Packet]) -> Picture | None:
        import av

        latest, captured, received = None, 0.0, 0.0
        for _, data, arrival, stamp in batch:
            self.sequence += 1
            sequence = self.sequence
            self.stamps[sequence] = (stamp, arrival)
            # Bound metadata even when pre-keyframe input yields no pictures.
            while len(self.stamps) > self.stamp_limit:
                del self.stamps[next(iter(self.stamps))]
            packet = av.Packet(data)
            packet.pts = sequence
            packet.time_base = Fraction(1, 1000000)
            try:
                frames = self.codec.decode(packet)
            except av.error.FFmpegError:
                self.stamps.pop(sequence, None)
                continue
            for frame in frames:
                matched = self.stamps.pop(frame.pts, None) if frame.pts is not None else None
                if matched is None:
                    raise RuntimeError("Decoded picture has no matching packet timestamp")
                latest = frame
                captured, received = matched
        if latest is None:
            return None
        return latest.to_ndarray(format=self.image_format), captured, received


class FirmwareLink:
    """Serial compressed decoding with bounded storage and newest-picture delivery."""

    def __init__(
        self,
        ip: str,
        *,
        on_frame: FrameSink,
        trace: Trace | None = None,
        on_failure: FailureSink | None = None,
        image_format: Literal["gray", "bgr24"] = "gray",
        max_packet_bytes: int = 8 << 20,
        max_packets: int = 256,
        max_queue_age: float = 0.4,
        batch_packets: int = 8,
        batch_bytes: int = 1 << 20,
        decoder_threads: int = 0,
    ) -> None:
        """Configure provisional queue limits; gray is the tracking-only default.

        Limits include active compressed batches. Native codec memory, OS socket buffers,
        one native decode result, and the pending/in-use decoded pictures are separate.
        ``on_failure`` must release the tracking owner; reconnect never restores a lock.
        Select ``bgr24`` for consumers requiring the previous three-channel image contract.
        """
        if image_format not in ("gray", "bgr24"):
            raise ValueError("image_format must be gray or bgr24")
        if min(max_packet_bytes, max_packets, batch_packets, batch_bytes) <= 0:
            raise ValueError("Video buffer limits must be positive")
        if not math.isfinite(max_queue_age) or max_queue_age <= 0 or decoder_threads < 0:
            raise ValueError("Invalid queue age or decoder thread count")
        self.ip, self.image_format = ip, image_format
        self._on_frame, self._on_failure, self._trace = on_frame, on_failure, trace
        self.max_packet_bytes, self.max_packets = max_packet_bytes, max_packets
        self.max_queue_age, self.batch_packets, self.batch_bytes = (
            max_queue_age,
            batch_packets,
            batch_bytes,
        )
        self.decoder_threads = decoder_threads
        self.client: FirmwareTrackingClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._packets: deque[Packet] = deque()
        self._packet_bytes = self._packet_count = 0
        self.peak_packet_bytes = self.peak_packets = self.superseded_frames = 0
        self._clock = CaptureClock()
        self._stats = _VideoStats()
        self._wake, self._frame_wake, self._failed = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self._latest: Picture | None = None
        self._decoder: _Decoder | None = None
        self._native: asyncio.Task[Picture | None] | None = None
        self._problem: Exception | None = None
        self._epoch = 0
        self._last_packet = self.last_frame_time = 0.0
        self.error: str | None = None

    @property
    def ready(self) -> bool:
        """Whether this session has recently delivered a valid picture."""
        return (
            self._problem is None
            and self.client is not None
            and self.client.is_connected
            and time.monotonic() - self.last_frame_time < 1.0
        )

    def start(self) -> None:
        """Start supervision, refusing reuse while a previous native job is alive."""
        if self._native is not None and not self._native.done():
            raise RuntimeError("Previous decoder is still running")
        if self._native is not None:
            with contextlib.suppress(Exception):
                self._native.result()
            self._native = None
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Invalidate delivery, stop supervision, and switch AI mode off."""
        self._epoch += 1
        self._latest = None
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._close_client()

    def _note(self, event: str, **fields: Any) -> None:  # noqa: ANN401
        if self._trace is not None:
            with contextlib.suppress(Exception):
                self._trace(event, fields)

    def _fail(self, error: Exception) -> None:
        if self._problem is None:
            self._problem = error
            self.error = f"Firmware link: {error}"
            self._epoch += 1
            self._latest = None
            self.last_frame_time = 0.0
            self._failed.set()
            self._wake.set()
            self._frame_wake.set()

    def _queue(self, index: int, data: bytes, arrival: float) -> None:
        if self._problem is not None:
            return
        self._last_packet = arrival
        if (
            self._packet_bytes + len(data) > self.max_packet_bytes
            or self._packet_count + 1 > self.max_packets
        ):
            self._fail(BufferError("Compressed video buffer limit exceeded"))
            return
        if time.monotonic() - arrival > self.max_queue_age:
            self._fail(TimeoutError("Compressed video exceeded queue age"))
            return
        self._stats.packet(len(data), arrival)
        self._packets.append((index, data, arrival, self._clock.update(index, arrival)))
        self._packet_bytes += len(data)
        self._packet_count += 1
        self.peak_packet_bytes = max(self.peak_packet_bytes, self._packet_bytes)
        self.peak_packets = max(self.peak_packets, self._packet_count)
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

    async def _monitor(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._failed.wait(), 0.2)
            if self._problem is not None:
                raise self._problem
            if self.client is None or not self.client.is_connected:
                raise ConnectionError("Tracking connection closed")
            now = time.monotonic()
            if now - self._last_packet > VIDEO_STALL:
                raise TimeoutError(f"No camera video for {VIDEO_STALL:g} s")
            if self._packets and now - self._packets[0][2] > self.max_queue_age:
                raise TimeoutError("Compressed video exceeded queue age")
            if now - self._stats.started >= 1.0:
                self._note(
                    "video_stats",
                    **self._stats.summary(now),
                    queue_bytes=self._packet_bytes,
                    queue_packets=self._packet_count,
                    superseded_frames=self.superseded_frames,
                )
                self._stats = _VideoStats()

    async def _drain_native(self) -> bool:
        job = self._native
        if job is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(job), 1.0)
        except TimeoutError:
            self.error = "Firmware link: decoder still running after shutdown timeout"
            self._note("decoder_shutdown_timeout")
            return False
        except Exception:
            pass  # failure is already owned by supervision
        self._native = None
        return True

    async def _run(self) -> None:
        while True:
            workers: list[asyncio.Task[None]] = []
            drained = True
            try:
                self._decoder = _Decoder(self.image_format, self.decoder_threads, self.max_packets)
                self._packets.clear()
                self._packet_bytes = self._packet_count = 0
                self._clock, self._stats = CaptureClock(), _VideoStats()
                self._problem = None
                self.error = None
                self._failed.clear()
                self._latest = None
                self._epoch += 1
                self.last_frame_time = 0.0
                client = FirmwareTrackingClient(self.ip, trace=self._trace, on_video=self._queue)
                self.client = client
                await client.connect()
                await client.stop_video()
                await asyncio.sleep(0.3)
                if await client.get_mode():
                    self._note("ai_mode_was_left_on")
                    with contextlib.suppress(Exception):
                        await client.cancel_target()
                        await asyncio.sleep(CANCEL_SETTLE)
                await client.set_mode(False)
                await client.start_video()
                self._last_packet = time.monotonic()
                workers = [
                    asyncio.create_task(fn())
                    for fn in (self._decode_loop, self._consume_loop, self._monitor)
                ]
                done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
                for worker in done:
                    worker.result()
                raise ConnectionError("Video worker stopped")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._fail(exc)
                self._note("link_error", error=repr(exc))
                logger.warning(self.error)
                if self._on_failure is not None:
                    try:
                        await self._on_failure(self.error or str(exc))
                    except Exception as failure:
                        self._note("owner_release_failed", error=repr(failure))
            finally:
                self._epoch += 1
                self._latest = None
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                drained = await self._drain_native()
                await self._close_client()
            if not drained:
                # A cancelled to_thread await cannot stop native code. Fail closed;
                # start() refuses a new session until the retained job actually finishes.
                return
            await asyncio.sleep(RETRY_DELAY)

    async def _decode_loop(self) -> None:
        decoder = self._decoder
        assert decoder is not None
        epoch = self._epoch
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._problem is not None:
                raise self._problem
            while self._packets:
                if time.monotonic() - self._packets[0][2] > self.max_queue_age:
                    raise TimeoutError("Compressed video exceeded queue age")
                batch: list[Packet] = []
                size = 0
                while self._packets and len(batch) < self.batch_packets:
                    packet = self._packets[0]
                    if batch and size + len(packet[1]) > self.batch_bytes:
                        break
                    batch.append(self._packets.popleft())
                    size += len(packet[1])
                started = time.monotonic()
                job = self._native = asyncio.create_task(asyncio.to_thread(decoder.decode, batch))
                picture = await asyncio.shield(job)
                self._native = None
                self._packet_bytes -= size
                self._packet_count -= len(batch)
                if epoch != self._epoch or self._problem is not None:
                    return
                if time.monotonic() - batch[-1][2] > self.max_queue_age:
                    raise TimeoutError("Decoded video exceeded processing age")
                elapsed = (time.monotonic() - started) * 1000
                self._stats.delivery(elapsed, (batch[-1][2] - batch[-1][3]) * 1000, len(batch))
                self._note(
                    "video_delivered",
                    packets=len(batch),
                    first=batch[0][0],
                    last=batch[-1][0],
                    decode_ms=round(elapsed, 1),
                    age_ms=round((time.monotonic() - batch[-1][2]) * 1000, 1),
                    lag_ms=round((batch[-1][2] - batch[-1][3]) * 1000, 1),
                    picture=picture is not None,
                )
                if picture is not None:
                    if self._latest is not None:
                        self.superseded_frames += 1
                    self._latest = picture
                    self._frame_wake.set()

    async def _consume_loop(self) -> None:
        epoch = self._epoch
        while True:
            await self._frame_wake.wait()
            self._frame_wake.clear()
            if epoch != self._epoch or self._problem is not None:
                return
            picture, self._latest = self._latest, None
            if picture is not None:
                if time.monotonic() - picture[2] > self.max_queue_age:
                    self._fail(TimeoutError("Decoded picture exceeded delivery age"))
                    return
                self.last_frame_time = time.monotonic()
                await self._on_frame(picture[0], picture[1])
