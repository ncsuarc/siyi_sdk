"""Buffered SIYI parsing with recoverable per-candidate errors."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from ..constants import CRC_LEN, HEADER_LEN, STX_BYTES
from ..exceptions import CRCError, FramingError, ProtocolError
from .crc import crc16
from .frame import Frame


@dataclass(slots=True)
class ParseResult:
    """Valid frames and malformed candidates encountered in one feed."""

    frames: list[Frame] = field(default_factory=list)
    errors: list[ProtocolError] = field(default_factory=list)


class FrameParser:
    """Extract frames without discarding valid neighbors of corrupt candidates."""

    def __init__(self, max_payload: int = 4096) -> None:
        """Set the payload limit and initialize the partial-frame buffer."""
        if not 0 <= max_payload <= 65535:
            raise ValueError("max_payload must be between 0 and 65535")
        self.max_payload = max_payload
        self._buffer = bytearray()

    def reset(self) -> None:
        """Discard partial data from the previous connection."""
        self._buffer.clear()

    def feed(self, chunk: bytes) -> ParseResult:
        """Return complete frames and errors, retaining a partial trailing frame."""
        self._buffer.extend(chunk)
        result = ParseResult()
        offset = 0
        size = len(self._buffer)
        while offset < size:
            start = self._buffer.find(STX_BYTES, offset)
            if start < 0:
                offset = size - int(self._buffer[-1] == STX_BYTES[0])
                break
            offset = start
            if size - start < HEADER_LEN:
                break
            ctrl, data_len, seq, cmd_id = struct.unpack_from("<BHHB", self._buffer, start + 2)
            if data_len > self.max_payload:
                result.errors.append(
                    FramingError(f"Payload size {data_len} exceeds maximum {self.max_payload}")
                )
                offset = start + 1
                continue
            end = start + HEADER_LEN + data_len + CRC_LEN
            if end > size:
                break
            expected = struct.unpack_from("<H", self._buffer, end - CRC_LEN)[0]
            actual = crc16(bytes(self._buffer[start : end - CRC_LEN]))
            if expected != actual:
                result.errors.append(CRCError(expected, actual, self._buffer[start:end].hex(" ")))
                offset = start + 1
                continue
            result.frames.append(
                Frame(
                    ctrl=ctrl,
                    seq=seq,
                    cmd_id=cmd_id,
                    data=bytes(self._buffer[start + HEADER_LEN : end - CRC_LEN]),
                )
            )
            offset = end
        if offset:
            del self._buffer[:offset]
        return result
