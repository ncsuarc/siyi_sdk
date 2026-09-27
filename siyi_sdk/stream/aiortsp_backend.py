# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""A8 H.264 RTSP sessions with bounded RTP and a serial PyAV worker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
import threading
import time
from collections import deque
from collections.abc import Mapping
from fractions import Fraction
from typing import Any, Protocol, cast
from urllib.parse import urlparse

import numpy as np
from numpy.typing import NDArray

try:
    import av
    from aiortsp.rtsp.connection import RTSPConnection
    from aiortsp.rtsp.session import RTSPMediaSession
    from aiortsp.transport import TCPTransport, UDPTransport

    _AIORTSP_AVAILABLE = True
except ImportError:
    _AIORTSP_AVAILABLE = False

from ._h264 import H264Assembler, JitterBuffer, RTPPacket, video_parameters
from ._threaded import ThreadedBackend
from .models import StreamConfig, StreamFrame, StreamState

_log = logging.getLogger(__name__)
_MAX_PACKETS = 1024
_MAX_BYTES = 4 * 1024 * 1024


class RTPData(Protocol):
    """Fields read from aiortsp's dpkt RTP packets."""

    pt: int
    seq: int
    ts: int
    m: int
    p: int
    x: int
    data: bytes


class AiortspBackend(ThreadedBackend):
    """Manage RTSP asynchronously and decode H.264 in one serial worker."""

    BACKEND_NAME = "aiortsp"

    def __init__(self, config: StreamConfig) -> None:
        """Configure an H.264 stream and its bounded RTP storage."""
        if not _AIORTSP_AVAILABLE:
            raise ImportError("Install aiortsp and av for this backend")
        if config.codec != "h264":
            raise ValueError("aiortsp supports H.264 only")
        super().__init__(config)
        self._condition = threading.Condition()
        self._packets: deque[RTPPacket] = deque()
        self._packet_bytes = 0
        self._jitter = JitterBuffer()
        self._epoch = 0
        self._decode_config: tuple[int, list[bytes]] | None = None
        self._damage = False
        self._payload_type: int | None = None
        self._session_task: asyncio.Task[None] | None = None
        self._last_decoded = 0.0
        self._healthy_since: float | None = None
        self._transport_error: Exception | None = None

    async def connect(self) -> None:
        """Start the decoder and the supervised RTSP session."""
        if self._session_task is not None and not self._session_task.done():
            raise RuntimeError("Previous RTSP session is still alive")
        await super().connect()
        self._session_task = asyncio.create_task(self._session_loop(), name="siyi-rtsp-session")

    async def disconnect(self) -> None:
        """Await session teardown and stop the owned decoder worker."""
        self._stop_event.set()
        with self._condition:
            self._condition.notify_all()
        if self._session_task is not None:
            self._session_task.cancel()
            await asyncio.gather(self._session_task, return_exceptions=True)
            self._session_task = None
        await super().disconnect()

    def handle_rtp(self, rtp: RTPData) -> None:
        """Enqueue a selected video packet within the combined storage limits."""
        if rtp.pt != self._payload_type or self._stop_event.is_set():
            return
        payload = bytes(rtp.data)
        # dpkt strips CSRC entries but leaves RFC 3550 header extensions in data.
        if rtp.x:
            if len(payload) < 4:
                return
            extension_size = 4 + struct.unpack_from("!H", payload, 2)[0] * 4
            if extension_size > len(payload):
                return
            payload = payload[extension_size:]
        # dpkt leaves RTP padding in data. Strip it before NAL processing.
        if rtp.p:
            if not payload or not 0 < payload[-1] <= len(payload):
                return
            payload = payload[: -payload[-1]]
        packet = RTPPacket(rtp.seq, rtp.ts, bool(rtp.m), payload, rtp.pt)
        with self._condition:
            count = len(self._packets) + len(self._jitter.pending)
            size = self._packet_bytes + self._jitter.bytes
            if count >= _MAX_PACKETS or size + len(payload) > _MAX_BYTES:
                self._packets.clear()
                self._packet_bytes = 0
                self._jitter.reset()
                self._damage = True
            if len(payload) > _MAX_BYTES:
                return
            self._packets.append(packet)
            self._packet_bytes += len(payload)
            self._condition.notify()

    def handle_rtcp(self, rtcp: object) -> None:
        """RTCP statistics are managed by the aiortsp transport."""
        pass

    def handle_closed(self, error: Exception | None) -> None:
        """Notify the supervisor of transport termination."""
        self._transport_error = error or OSError("RTP transport closed")

    def _configure(self, sdp: Mapping[str, Any]) -> None:
        payload_type, mode, parameters = video_parameters(sdp)
        with self._condition:
            self._packets.clear()
            self._packet_bytes = 0
            self._jitter.reset()
            self._epoch += 1
            self._decode_config = (mode, parameters)
            self._payload_type = payload_type
            self._damage = False
            self._condition.notify()

    def _worker(self) -> None:
        epoch = -1
        assembler = None
        codec = None
        while not self._stop_event.is_set():
            with self._condition:
                if not self._packets:
                    self._condition.wait(0.010 if self._jitter.pending else None)
                if self._stop_event.is_set():
                    return
                if self._decode_config is None:
                    continue
                if epoch != self._epoch:
                    epoch = self._epoch
                    mode, parameters = self._decode_config
                    assembler = H264Assembler(mode, parameters)
                    codec = av.CodecContext.create("h264", "r")
                    codec.thread_count = 1
                damage, self._damage = self._damage, False
                packet = self._packets.popleft() if self._packets else None
                if packet is not None:
                    self._packet_bytes -= len(packet.payload)
                ordered = self._jitter.feed(packet, time.monotonic())
            assert assembler is not None and codec is not None
            if damage:
                assembler.loss()
                codec = av.CodecContext.create("h264", "r")
                codec.thread_count = 1
            for packet, gap in ordered:
                if gap:
                    codec = av.CodecContext.create("h264", "r")
                    codec.thread_count = 1
                access_unit = assembler.feed(packet, gap)
                if access_unit is None:
                    continue
                try:
                    encoded = av.Packet(access_unit)
                    encoded.pts = packet.timestamp
                    encoded.time_base = Fraction(1, 90000)
                    decoded = codec.decode(encoded)
                    for image in decoded:
                        img = image.to_ndarray(format="bgr24")
                        now = time.monotonic()
                        if epoch != self._epoch or self._stop_event.is_set():
                            break
                        self._last_decoded = now
                        if self._healthy_since is None:
                            self._healthy_since = now
                        self.state = StreamState.RUNNING
                        if self._delivery:
                            self._delivery.publish(
                                StreamFrame(
                                    cast(NDArray[np.uint8], img),
                                    now,
                                    image.width,
                                    image.height,
                                    self.BACKEND_NAME,
                                )
                            )
                except av.FFmpegError:
                    assembler.loss()
                    codec = av.CodecContext.create("h264", "r")
                    codec.thread_count = 1

    async def _session_loop(self) -> None:
        attempts = 0
        delay = self._config.reconnect_delay
        error: Exception = OSError("RTSP session failed")
        try:
            while not self._stop_event.is_set():
                try:
                    await self._session()
                    error = OSError("RTSP session ended")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = exc
                if self._stop_event.is_set():
                    return
                if (
                    self._healthy_since is not None
                    and self._last_decoded - self._healthy_since >= 30
                ):
                    attempts, delay = 0, self._config.reconnect_delay
                if (
                    self._config.max_reconnect_attempts
                    and attempts >= self._config.max_reconnect_attempts
                ):
                    raise error
                self.state = StreamState.RECONNECTING
                await asyncio.sleep(delay)
                attempts += 1
                delay = min(delay * 2, 30)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = exc
            self.state = StreamState.FAILED
            if self._delivery:
                self._delivery.close(exc)

    async def _session(self) -> None:
        url = urlparse(self._config.rtsp_url)
        if url.scheme not in ("rtsp", "rtsps") or not url.hostname:
            raise ValueError("Expected a valid RTSP URL")
        import ssl

        conn = RTSPConnection(
            url.hostname,
            url.port or (322 if url.scheme == "rtsps" else 554),
            url.username,
            url.password,
            timeout=3,
            ssl=ssl.create_default_context() if url.scheme == "rtsps" else None,
        )
        transport_class = TCPTransport if self._config.transport == "tcp" else UDPTransport
        transport = transport_class(conn, timeout=5)
        session = RTSPMediaSession(conn, self._config.rtsp_url, transport=transport)
        self._transport_error = None
        self._healthy_since = None
        session_tasks = []
        try:
            await conn.__aenter__()
            await transport.__aenter__()
            await session.__aenter__()
            self._configure(session.sdp)
            transport.subscribe(self)
            await session.play()
            self._last_decoded = time.monotonic()
            last_keepalive = self._last_decoded
            while conn.running and transport.running and not self._stop_event.is_set():
                if self._transport_error is not None:
                    raise self._transport_error
                if self.last_error is not None:
                    raise self.last_error
                now = time.monotonic()
                if now - self._last_decoded >= 5:
                    raise TimeoutError("No decoded H.264 frame for five seconds")
                if now - last_keepalive >= session.session_keepalive:
                    await session.keep_alive()
                    last_keepalive = now
                await asyncio.sleep(0.05)
        finally:
            self._payload_type = None
            # aiortsp cancels these tasks but does not await them. Retain references.
            session_tasks = [
                task for task in (transport._rtcp_loop, transport._timeout_loop) if task is not None
            ]
            for task in session_tasks:
                task.cancel()
            with contextlib.suppress(Exception):
                if session.is_setup or session.session_id:
                    await asyncio.wait_for(session.teardown(), 3)
            with contextlib.suppress(KeyError):
                transport.unsubscribe(self)
            try:
                await transport.cleanup()
            finally:
                transport.close()
                conn.close()
                if session_tasks:
                    await asyncio.gather(*session_tasks, return_exceptions=True)
