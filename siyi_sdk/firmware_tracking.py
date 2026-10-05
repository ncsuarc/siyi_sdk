"""Experimental A8 Mini private tracking protocol (camera 0.3.7 / gimbal 0.4.9).

Recovered from the downloaded firmware; see docs/firmware-ai-tracking-analysis.md.
This is a separate TCP connection, not the public SDK protocol on port 37260.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from dataclasses import dataclass

from siyi_sdk.transport.base import AbstractTransport
from siyi_sdk.transport.tcp import TCPTransport

IO_TIMEOUT = 0.4
_MAGIC = b"\x55\x66\xaa\xbb"
_MAX_PAYLOAD = 4096 - 20  # receive buffer in the inspected camera firmware


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

    def feed(self, data: bytes) -> list[FirmwareFrame]:
        """Return complete packets; discard invalid headers, lengths and checksums."""
        self._buffer.extend(data)
        frames = []
        while len(self._buffer) >= 4:
            start = self._buffer.find(_MAGIC)
            if start < 0:
                del self._buffer[:-3]
                break
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
                del self._buffer[0]
                continue
            end = 16 + length
            if len(self._buffer) < end + 4:
                break
            if struct.unpack_from("<I", self._buffer, end)[0] != firmware_crc32(
                bytes(self._buffer[:end])
            ):
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
        self, ip: str = "192.168.144.25", *, transport: AbstractTransport | None = None
    ) -> None:
        """Use a dedicated port 37256 transport, or an injected transport."""
        self._transport = transport or TCPTransport(ip, 37256, connect_timeout=IO_TIMEOUT)
        self._reader: asyncio.Task[None] | None = None
        self._requests = asyncio.Lock()
        self._writes = asyncio.Lock()
        self._pending: tuple[int, bytes | None, asyncio.Future[bytes]] | None = None
        self._sequence = 0
        self.error: str | None = None

    @property
    def is_connected(self) -> bool:
        """Whether the private connection has a running reader and no known failure."""
        return self._transport.is_connected and self._reader is not None and not self.error

    async def connect(self) -> None:
        """Open the private channel; do not change the camera's mode."""
        if self._reader is not None:
            raise RuntimeError("Close the previous tracking connection before connecting")
        self.error = None
        await asyncio.wait_for(self._transport.connect(), IO_TIMEOUT)
        self._reader = asyncio.create_task(self._read())

    async def close(self) -> None:
        """Close transport only; callers must disable AI before relinquishing control."""
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        await asyncio.wait_for(self._transport.close(), IO_TIMEOUT)

    async def _read(self) -> None:
        parser = FirmwareFrameParser()
        try:
            async for chunk in self._transport.stream():
                for frame in parser.feed(chunk):
                    if frame.command == 0xAB and frame.payload == b"\x02":
                        self.error = "Camera rejected tracking coordinates: AI mode is disabled"
                    pending = self._pending
                    if pending and frame.flags & 2 and frame.command == pending[0]:
                        expected, future = pending[1:]
                        if not future.done() and (expected is None or frame.payload == expected):
                            future.set_result(frame.payload)
        except Exception as exc:
            self.error = f"Tracking connection failed: {exc}"
        finally:
            self.error = self.error or "Tracking connection closed"
            if self._pending and not self._pending[2].done():
                self._pending[2].set_exception(ConnectionError(self.error))

    async def _send(self, command: int, payload: bytes = b"", *, flags: int = 1) -> None:
        async with self._writes:
            self._sequence = (self._sequence + 1) & 0xFFFF
            await self._transport.send(encode_frame(command, payload, self._sequence, flags=flags))

    async def _request(
        self, command: int, payload: bytes = b"", expected: bytes | None = None
    ) -> bytes:
        async with self._requests:
            future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
            self._pending = command, expected, future
            try:
                await self._send(command, payload)
                return await future
            finally:
                self._pending = None
                future.cancel()

    async def get_mode(self) -> bool:
        """Query AI mode, rejecting unsupported or malformed replies."""
        result = await asyncio.wait_for(self._request(0xA2), IO_TIMEOUT)
        if result not in (b"\x00", b"\x01"):
            raise RuntimeError("Unsupported firmware AI mode response")
        return result == b"\x01"

    async def set_mode(self, enabled: bool) -> None:
        """Set AI mode and confirm it by a separate query, within 400 ms total."""

        async def change() -> None:
            value = bytes([enabled])
            await self._request(0xA3, value, expected=value)
            if await self._request(0xA2) != value:
                raise RuntimeError("Camera did not confirm the requested AI mode")

        await asyncio.wait_for(change(), IO_TIMEOUT)

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
