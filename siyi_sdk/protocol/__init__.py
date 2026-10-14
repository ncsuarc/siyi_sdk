"""Protocol layer for SIYI SDK.

This subpackage provides low-level protocol handling:
- CRC-16/XMODEM checksum calculation
- Frame encoding/decoding
- Streaming frame parser
"""

from __future__ import annotations

from .crc import crc16, crc16_check
from .frame import Frame
from .parser import FrameParser, ParseResult

__all__ = [
    "Frame",
    "FrameParser",
    "ParseResult",
    "crc16",
    "crc16_check",
]
