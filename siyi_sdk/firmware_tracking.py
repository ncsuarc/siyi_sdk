"""Experimental A8 Mini private tracking protocol (camera 0.3.7 / gimbal 0.4.9).

Recovered from the downloaded firmware; see docs/firmware-ai-tracking-analysis.md.
This is a separate TCP connection, not the public SDK protocol on port 37260.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from siyi_sdk.transport.base import AbstractTransport
from siyi_sdk.transport.tcp import TCPTransport

IO_TIMEOUT = 0.4
# Accepting a connection has taken up to ~400 ms on hardware; it precedes any mode change.
CONNECT_TIMEOUT = 1.5
# The camera drops a port 37256 client that has not sent 0x80 (record status query)
# for over 4 s (cardv thread_tcp_server_ex_sdk_parse); the SIYI AI module polls it on a timer.
KEEPALIVE_INTERVAL = 1.0
# Replies usually take 3-50 ms, but the camera has stalled for ~800 ms while otherwise healthy.
REPLY_TIMEOUT = 1.5
_MAGIC = b"\x55\x66\xaa\xbb"
_MAX_PAYLOAD = 4096 - 20  # receive buffer in the inspected camera firmware

# Diagnostic sink: called with an event name and its fields; must not raise.
Trace = Callable[[str, dict[str, Any]], None]


def _no_trace(event: str, fields: dict[str, Any]) -> None:
    pass


def _ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 1)


def firmware_crc32(data: bytes) -> int:
    """Return the firmware's MSB-first CRC32, seed 0, polynomial 0x04C11DB7."""
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
    return crc


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
                or length > _MAX_PAYLOAD
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
    ) -> None:
        """Use a dedicated port 37256 transport, or an injected transport.

        ``trace`` receives every frame, request outcome and connection event.
        """
        self._transport = transport or TCPTransport(ip, 37256, connect_timeout=CONNECT_TIMEOUT)
        self._reader: asyncio.Task[None] | None = None
        self._keepalive: asyncio.Task[None] | None = None
        self._requests = asyncio.Lock()
        self._writes = asyncio.Lock()
        self._pending: tuple[int, bytes | None, asyncio.Future[bytes]] | None = None
        self._sequence = 0
        self._trace = trace or _no_trace
        self.error: str | None = None

    def _emit(self, event: str, **fields: Any) -> None:
        with contextlib.suppress(Exception):
            self._trace(event, fields)

    def note(self, event: str, **fields: Any) -> None:
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
        self._emit("connected", ms=_ms(started))
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
        try:
            async for chunk in self._transport.stream():
                discarded = parser.discarded
                for frame in parser.feed(chunk):
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
