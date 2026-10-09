"""Experimental A8 Mini private tracking protocol (camera 0.3.7 / gimbal 0.4.9).

Recovered from the downloaded firmware; see docs/firmware-ai-tracking-analysis.md.
This is a separate TCP connection, not the public SDK protocol on port 37260.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import sys
import time
import zlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from siyi_sdk import constants
from siyi_sdk.transport.base import AbstractTransport
from siyi_sdk.transport.threaded_tcp import ThreadedTCPTransport

IO_TIMEOUT = 0.4
# Accepting a connection has taken up to ~400 ms on hardware; it precedes any mode change.
CONNECT_TIMEOUT = 1.5
# The camera drops a port 37256 client that has not sent 0x80 (record status query)
# for over 4 s (cardv thread_tcp_server_ex_sdk_parse); the SIYI AI module polls it on a timer.
KEEPALIVE_INTERVAL = 1.0
# Replies usually take 3-50 ms, but the camera has stalled for ~800 ms while otherwise healthy.
REPLY_TIMEOUT = 1.5
# The AI module sends a "canceled" target (status 3), waits 60 ms, then switches AI mode off.
CANCEL_SETTLE = 0.06
_MAGIC = b"\x55\x66\xaa\xbb"
_MAX_PAYLOAD = 4096 - 20  # receive buffer in the inspected camera firmware
# Received packets can be larger: 0x90 carries whole encoded frames (cardv caps them near 358 KB).
_MAX_RX_PAYLOAD = 384 * 1024
# One encoded video frame from the camera's 0x90 stream: (frame counter, H.265 Annex-B
# access unit, monotonic arrival time).
VideoSink = Callable[[int, bytes, float], None]

# Diagnostic sink: called with an event name and its fields; must not raise.
Trace = Callable[[str, dict[str, Any]], None]


def _no_trace(event: str, fields: dict[str, Any]) -> None:
    pass


def disable_delayed_ack(sock: Any) -> bool:  # noqa: ANN401 - any socket-like object
    """Make Windows acknowledge every received TCP segment at once.

    The camera appears to hold each video frame's last partial segment until
    earlier data is acknowledged (Nagle), while Windows delays acknowledgements
    by up to 200 ms; together that bunches the 0x90 stream into bursts. Returns
    whether the option was applied (Windows only).
    """
    if sys.platform != "win32" or sock is None:
        return False
    import ctypes
    from ctypes import wintypes

    sio_tcp_set_ack_frequency = constants.SIO_TCP_SET_ACK_FREQUENCY
    frequency = wintypes.DWORD(1)
    returned = wintypes.DWORD(0)
    wsa_ioctl = ctypes.windll.ws2_32.WSAIoctl
    wsa_ioctl.argtypes = [ctypes.c_size_t, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                          ctypes.c_void_p, ctypes.c_void_p]
    result = wsa_ioctl(sock.fileno(), sio_tcp_set_ack_frequency, ctypes.byref(frequency),
                       ctypes.sizeof(frequency), None, 0, ctypes.byref(returned), None, None)
    return bool(result == 0)


def _ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 1)


def _socket_details(sock: Any) -> dict[str, Any]:  # noqa: ANN401
    """Local address and buffer sizes of a connected socket, for the trace (best effort)."""
    import socket

    details: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        details["local"] = "{}:{}".format(*sock.getsockname()[:2])
        details["rcvbuf"] = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        details["nodelay"] = bool(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))
    return details


async def _stamped(chunks: AsyncIterator[bytes]) -> AsyncIterator[tuple[bytes, float]]:
    """Pair chunks from a transport that doesn't stamp them with the time each was read."""
    async for chunk in chunks:
        yield chunk, time.monotonic()


# Bit-reversal of every byte value, to compute the MSB-first CRC with zlib's LSB-first one.
_REVERSED_BYTES = bytes(int(f"{value:08b}"[::-1], 2) for value in range(256))


def firmware_crc32(data: bytes) -> int:
    """Return the firmware's MSB-first CRC32, seed 0, polynomial 0x04C11DB7.

    zlib computes the same polynomial bit-reflected with an inverted register;
    reflecting the input bytes and the result, and undoing the inversion, gives
    the firmware's CRC in C. A pure-Python loop took ~7 ms per 5 KB video frame,
    enough to make the dashboard fall about a second behind the 0x90 stream.
    """
    mask = constants.CRC32_XOR_MASK
    register = zlib.crc32(bytes(data).translate(_REVERSED_BYTES), mask) ^ mask
    return int(f"{register:032b}"[::-1], 2)


def encode_frame(command: int, payload: bytes, sequence: int, *, flags: int = 1) -> bytes:
    """Encode a private long-protocol frame, including header and whole-frame CRCs."""
    if not 0 <= flags <= 3 or len(payload) > _MAX_PAYLOAD:
        raise ValueError("Invalid private protocol flags or payload length")
    header = _MAGIC + struct.pack("<BIHB", flags, len(payload), sequence, command)
    packet = header + struct.pack("<I", firmware_crc32(header)) + payload
    return packet + struct.pack("<I", firmware_crc32(packet))


@dataclass(frozen=True)
class FirmwareFrame:
    """One validated private-protocol packet."""

    command: int
    payload: bytes
    sequence: int
    flags: int


class FirmwareFrameParser:
    """Handle split/coalesced TCP reads and recover after corrupt packets."""

    def __init__(self) -> None:
        """Start with an empty receive buffer."""
        self._buffer = bytearray()
        self.discarded = 0  # bytes dropped while resynchronizing, for diagnostics

    def feed(self, data: bytes) -> list[FirmwareFrame]:
        """Return complete packets; discard invalid headers, lengths and checksums."""
        self._buffer.extend(data)
        frames = []
        while len(self._buffer) >= 4:
            start = self._buffer.find(_MAGIC)
            if start < 0:
                self.discarded += len(self._buffer) - 3
                del self._buffer[:-3]
                break
            self.discarded += start
            del self._buffer[:start]
            if len(self._buffer) < 16:
                break
            flags, length, sequence, command = struct.unpack_from("<BIHB", self._buffer, 4)
            header_crc = struct.unpack_from("<I", self._buffer, 12)[0]
            if (
                flags > 3
                or length > _MAX_RX_PAYLOAD
                or header_crc != firmware_crc32(bytes(self._buffer[:12]))
            ):
                self.discarded += 1
                del self._buffer[0]
                continue
            end = 16 + length
            if len(self._buffer) < end + 4:
                break
            if struct.unpack_from("<I", self._buffer, end)[0] != firmware_crc32(
                bytes(self._buffer[:end])
            ):
                self.discarded += 1
                del self._buffer[0]
                continue
            frames.append(FirmwareFrame(command, bytes(self._buffer[16:end]), sequence, flags))
            del self._buffer[: end + 4]
        return frames


class FirmwareTrackingClient:
    """Bounded private tracking I/O, with no retries or automatic reconnection.

    Replies use the camera's own sequence counter. Requests are serialized and
    matched by command and expected mode byte, not by echoed sequence numbers.
    A successful target write is not an acknowledgement of physical movement.
    """

    def __init__(
        self,
        ip: str = "192.168.144.25",
        *,
        transport: AbstractTransport | None = None,
        trace: Trace | None = None,
        on_video: VideoSink | None = None,
    ) -> None:
        """Use a dedicated port 37256 transport, or an injected transport.

        ``trace`` receives every frame, request outcome and connection event.
        ``on_video`` receives the camera's 0x90 video frames once
        :meth:`start_video` has requested them (they are not traced one by one).
        The default transport reads in its own thread, as the AI module does, so the
        arrival time passed to ``on_video`` is when the data came in, not when the
        event loop got to it.
        """
        self._transport = transport or ThreadedTCPTransport(
            ip, 37256, connect_timeout=CONNECT_TIMEOUT
        )
        self._reader: asyncio.Task[None] | None = None
        self._keepalive: asyncio.Task[None] | None = None
        self._requests = asyncio.Lock()
        self._writes = asyncio.Lock()
        self._pending: tuple[int, bytes | None, asyncio.Future[bytes]] | None = None
        self._sequence = 0
        self._trace = trace or _no_trace
        self._on_video = on_video
        self.video_frames = 0
        self.error: str | None = None

    def _emit(self, event: str, **fields: Any) -> None:  # noqa: ANN401
        with contextlib.suppress(Exception):
            self._trace(event, fields)

    def note(self, event: str, **fields: Any) -> None:  # noqa: ANN401
        """Record a caller's diagnostic event in this connection's trace."""
        self._emit(event, **fields)

    @property
    def is_connected(self) -> bool:
        """Whether the private connection has a running reader and no known failure."""
        return self._transport.is_connected and self._reader is not None and not self.error

    async def connect(self) -> None:
        """Open the private channel; do not change the camera's mode."""
        if self._reader is not None:
            raise RuntimeError("Close the previous tracking connection before connecting")
        self.error = None
        started = time.monotonic()
        try:
            await asyncio.wait_for(self._transport.connect(), CONNECT_TIMEOUT)
        except TimeoutError:
            self._emit("connect_failed", ms=_ms(started), error="timeout")
            raise TimeoutError(
                "Camera did not accept the tracking connection (port 37256) within "
                f"{CONNECT_TIMEOUT:g} s"
            ) from None
        except Exception as exc:
            self._emit("connect_failed", ms=_ms(started), error=repr(exc))
            raise
        quick_ack = False
        with contextlib.suppress(Exception):
            quick_ack = disable_delayed_ack(getattr(self._transport, "socket", None))
        self._emit("connected", ms=_ms(started), quick_ack=quick_ack,
                   **_socket_details(getattr(self._transport, "socket", None)))
        self._reader = asyncio.create_task(self._read())
        self._keepalive = asyncio.create_task(self._keep_alive())

    async def _keep_alive(self) -> None:
        """Send the AI module's record-status query so the camera keeps this client."""
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            try:
                await asyncio.wait_for(self._send(0x80, b"\x01"), IO_TIMEOUT)
            except Exception as exc:
                self._emit("keepalive_failed", error=repr(exc))
                return

    async def close(self) -> None:
        """Close transport only; callers must disable AI before relinquishing control.

        The camera serves one client on this port and blocks on it until it
        disconnects, so the connection is reset rather than closed gracefully:
        a graceful close waits on a stalled camera and can leave it half-open,
        locking every later client out until the camera restarts.
        """
        keepalive, self._keepalive = self._keepalive, None
        if keepalive is not None:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        abort = getattr(self._transport, "abort", None)
        self._emit("close", method="reset" if abort is not None else "graceful")
        if abort is not None:
            await abort()
        else:
            await asyncio.wait_for(self._transport.close(), IO_TIMEOUT)

    async def _read(self) -> None:
        parser = FirmwareFrameParser()
        how = "eof"  # how the stream ended, for the trace
        # A transport that reads in its own thread stamps each chunk when it arrives; otherwise
        # stamp it as it is taken from the stream.
        timed = getattr(self._transport, "stream_timed", None)
        chunks = timed() if timed is not None else _stamped(self._transport.stream())
        try:
            async for chunk, arrival in chunks:
                discarded = parser.discarded
                for frame in parser.feed(chunk):
                    if frame.command == 0x90 and len(frame.payload) > 6:
                        # 6-byte header: little-endian frame counter, then 01 00.
                        if self.video_frames == 0:
                            self._emit("video_started", header=frame.payload[:6].hex(" "))
                        self.video_frames += 1
                        self._emit("video_packet", n=struct.unpack_from("<I", frame.payload)[0],
                                   bytes=len(frame.payload) - 6, chunk=len(chunk))
                        if self._on_video is not None:
                            index = struct.unpack_from("<I", frame.payload)[0]
                            with contextlib.suppress(Exception):
                                self._on_video(index, frame.payload[6:], arrival)
                        continue
                    self._emit(
                        "rx", cmd=f"0x{frame.command:02X}", seq=frame.sequence, flags=frame.flags,
                        len=len(frame.payload), payload=frame.payload[:64].hex(" "),
                    )
                    if frame.command == 0xAB and frame.payload == b"\x02":
                        self.error = "Camera rejected tracking coordinates: AI mode is disabled"
                    pending = self._pending
                    if pending and frame.flags & 2 and frame.command == pending[0]:
                        expected, future = pending[1:]
                        if not future.done() and (expected is None or frame.payload == expected):
                            future.set_result(frame.payload)
                if parser.discarded != discarded:
                    self._emit("rx_discarded", bytes=parser.discarded - discarded,
                               total=parser.discarded)
        except asyncio.CancelledError:
            how = "closed by app"
            raise
        except Exception as exc:
            how = f"error: {exc!r}"
            self.error = f"Tracking connection failed: {exc}"
        finally:
            self._emit("reader_end", how=how, error=self.error)
            self.error = self.error or "Tracking connection closed"
            if self._pending and not self._pending[2].done():
                self._pending[2].set_exception(ConnectionError(self.error))

    async def _send(self, command: int, payload: bytes = b"", *, flags: int = 1) -> None:
        async with self._writes:
            self._sequence = (self._sequence + 1) & 0xFFFF
            self._emit("tx", cmd=f"0x{command:02X}", seq=self._sequence, flags=flags,
                       payload=payload.hex(" "))
            await self._transport.send(encode_frame(command, payload, self._sequence, flags=flags))

    async def _request(
        self, command: int, payload: bytes = b"", expected: bytes | None = None
    ) -> bytes:
        async with self._requests:
            future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
            self._pending = command, expected, future
            started = time.monotonic()
            outcome = "no reply"
            try:
                await self._send(command, payload)
                result = await future
                outcome = "reply"
                return result
            except Exception as exc:
                outcome = f"failed: {exc!r}"
                raise
            finally:
                # Cancellation (the caller's timeout) leaves outcome as "no reply".
                self._emit("request", cmd=f"0x{command:02X}", outcome=outcome, ms=_ms(started))
                self._pending = None
                future.cancel()

    async def get_mode(self) -> bool:
        """Query AI mode, rejecting unsupported or malformed replies."""
        try:
            result = await asyncio.wait_for(self._request(0xA2), REPLY_TIMEOUT)
        except TimeoutError:
            raise TimeoutError(f"No AI mode reply within {REPLY_TIMEOUT:g} s") from None
        if result not in (b"\x00", b"\x01"):
            raise RuntimeError("Unsupported firmware AI mode response")
        return result == b"\x01"

    async def set_mode(self, enabled: bool) -> None:
        """Set AI mode and confirm it by a separate query, within REPLY_TIMEOUT in total."""

        async def change() -> None:
            value = bytes([enabled])
            await self._request(0xA3, value, expected=value)
            if await self._request(0xA2) != value:
                raise RuntimeError("Camera did not confirm the requested AI mode")

        try:
            await asyncio.wait_for(change(), REPLY_TIMEOUT)
        except TimeoutError:
            state = "enable" if enabled else "disable"
            raise TimeoutError(
                f"Camera did not confirm AI {state} within {REPLY_TIMEOUT:g} s"
            ) from None

    async def start_video(self) -> None:
        """Ask the camera for its 1280x720 H.265 stream (0x90 on, as the AI module does).

        The camera ignores the request unless it is flagged as needing a reply;
        its first video frame is that reply.
        """
        await asyncio.wait_for(self._send(0x90, bytes([1])), IO_TIMEOUT)

    async def stop_video(self) -> None:
        """Stop the 0x90 stream."""
        await asyncio.wait_for(self._send(0x90, bytes([0])), IO_TIMEOUT)

    async def send_target(self, x: int, y: int, width: int, height: int) -> None:
        """Send one 1280x720 target, any-object type/state (255/4), without autozoom."""
        if not self.is_connected:
            raise ConnectionError(self.error or "Tracking channel is not connected")
        if not (0 <= x < 1280 and 0 <= y < 720 and 1 <= width <= 1280 and 1 <= height <= 720):
            raise ValueError("Target lies outside the firmware's 1280x720 coordinate space")
        await asyncio.wait_for(
            self._send(0xAB, struct.pack("<HHHHBB", x, y, width, height, 255, 4), flags=0),
            IO_TIMEOUT,
        )

    async def cancel_target(self) -> None:
        """Send the "canceled" target (status 3) the AI module sends before switching AI off.

        Position and size are zero and the type is the any-object value this client's targets
        carry. Only valid while AI mode is on: with it off the camera answers a target with a
        one-byte ``02``, which marks this connection failed.
        """
        if not self.is_connected:
            raise ConnectionError(self.error or "Tracking channel is not connected")
        await asyncio.wait_for(
            self._send(0xAB, struct.pack("<HHHHBB", 0, 0, 0, 0, 255, 3), flags=0), IO_TIMEOUT
        )
