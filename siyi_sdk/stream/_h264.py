"""Non-interleaved H.264 RTP assembly (RFC 6184, packetization modes 0/1)."""

from __future__ import annotations

import base64
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

START_CODE = b"\x00\x00\x00\x01"
MAX_ACCESS_UNIT = 8 * 1024 * 1024
RTP_HALF_RANGE = 32768


@dataclass(slots=True)
class RTPPacket:
    seq: int
    timestamp: int
    marker: bool
    payload: bytes
    pt: int


def video_parameters(sdp: Mapping[str, Any]) -> tuple[int, int, list[bytes]]:
    media = next((m for m in sdp.get("medias", []) if m.get("type") == "video"), None)
    if media is None:
        raise ValueError("SDP contains no video media")
    attributes = media.get("attributes", {})
    mapping = attributes.get("rtpmap", {})
    if mapping.get("encoding", "").upper() != "H264":
        raise ValueError("aiortsp supports H.264 only; H.265 and other codecs are unsupported")
    pt = int(mapping["pt"])
    if mapping.get("clockRate") != 90000:
        raise ValueError("H.264 RTP clock must be 90000 Hz")
    fmtp = attributes.get("fmtp", {})
    if fmtp and int(fmtp.get("pt", -1)) != pt:
        raise ValueError("SDP fmtp does not describe the selected video payload")
    mode = int(fmtp.get("packetization-mode", 0))
    if mode not in (0, 1):
        raise ValueError("Interleaved H.264 packetization is unsupported")
    parameters = []
    for value in fmtp.get("sprop-parameter-sets", "").split(","):
        if value:
            nal = base64.b64decode(value, validate=True)
            if not nal or nal[0] & 31 not in (7, 8):
                raise ValueError("Invalid SDP H.264 parameter set")
            parameters.append(nal)
    return pt, mode, parameters


class JitterBuffer:
    """Reorder at most 64 packets, waiting at most 50 ms for a gap."""

    def __init__(self) -> None:
        self.pending: dict[int, tuple[RTPPacket, float]] = {}
        self.expected: int | None = None
        self.bytes = 0

    def reset(self) -> None:
        self.pending.clear()
        self.expected = None
        self.bytes = 0

    def feed(self, packet: RTPPacket | None, now: float) -> list[tuple[RTPPacket, bool]]:
        if packet is not None:
            if self.expected is None:
                self.expected = packet.seq
            distance = (packet.seq - self.expected) & 0xFFFF
            if distance < RTP_HALF_RANGE and packet.seq not in self.pending:
                self.pending[packet.seq] = (packet, now)
                self.bytes += len(packet.payload)
        output = []
        gap = False
        while self.pending:
            assert self.expected is not None
            expected = self.expected
            item = self.pending.pop(self.expected, None)
            if item is None:
                oldest = min(arrival for _, arrival in self.pending.values())
                furthest = max((seq - expected) & 0xFFFF for seq in self.pending)
                if len(self.pending) < 64 and furthest < 64 and now - oldest < 0.050:
                    break
                self.expected = min(self.pending, key=lambda seq: (seq - expected) & 0xFFFF)
                gap = True
                continue
            pkt, _ = item
            self.bytes -= len(pkt.payload)
            output.append((pkt, gap))
            gap = False
            self.expected = (self.expected + 1) & 0xFFFF
        return output


class H264Assembler:
    def __init__(self, mode: int = 1, parameters: list[bytes] | None = None) -> None:
        if mode not in (0, 1):
            raise ValueError("Unsupported packetization mode")
        self.mode = mode
        self.parameters = {nal[0] & 31: nal for nal in parameters or []}
        self.need_keyframe = True
        self.timestamp: int | None = None
        self.data = bytearray()
        self.fu: bytearray | None = None
        self.fu_header: int | None = None
        self.damaged = False
        self.types: set[int] = set()

    def loss(self) -> None:
        self.need_keyframe = True
        self.damaged = True
        self.data.clear()
        self.fu = None

    def _nal(self, nal: bytes) -> None:
        if not nal or nal[0] & 0x80 or not 1 <= (nal[0] & 31) <= 23:
            raise ValueError("Invalid H.264 NAL unit")
        kind = nal[0] & 31
        if len(self.data) + len(nal) + 4 > MAX_ACCESS_UNIT:
            raise ValueError("H.264 access unit exceeds 8 MiB")
        self.data.extend(START_CODE)
        self.data.extend(nal)
        self.types.add(kind)
        if kind in (7, 8):
            self.parameters[kind] = nal

    def feed(self, packet: RTPPacket, gap: bool = False) -> bytes | None:
        if self.timestamp != packet.timestamp:
            if self.data or self.fu is not None:
                self.need_keyframe = True  # Prior access unit never reached its marker.
            self.timestamp = packet.timestamp
            self.data.clear()
            self.fu = None
            self.types.clear()
            self.damaged = False
        if gap:
            self.loss()
        if self.damaged:
            return None
        try:
            payload = packet.payload
            if not payload or payload[0] & 0x80:
                raise ValueError("Empty or forbidden RTP NAL")
            kind = payload[0] & 31
            if 1 <= kind <= 23:
                if self.fu is not None:
                    raise ValueError("Unfinished FU-A")
                self._nal(payload)
            elif self.mode == 1 and kind == 24:
                if self.fu is not None:
                    raise ValueError("Unfinished FU-A before STAP-A")
                offset = 1
                while offset < len(payload):
                    if offset + 2 > len(payload):
                        raise ValueError("Truncated STAP-A length")
                    size = struct.unpack_from("!H", payload, offset)[0]
                    offset += 2
                    if not size or offset + size > len(payload):
                        raise ValueError("Truncated STAP-A NAL")
                    self._nal(payload[offset : offset + size])
                    offset += size
            elif self.mode == 1 and kind == 28:
                if len(payload) < 3:
                    raise ValueError("Truncated FU-A")
                indicator, header = payload[:2]
                start, end = bool(header & 0x80), bool(header & 0x40)
                nal_header = (indicator & 0xE0) | (header & 31)
                if header & 0x20 or (start and end) or not 1 <= (header & 31) <= 23:
                    raise ValueError("Invalid FU-A header")
                if start:
                    if self.fu is not None:
                        raise ValueError("Overlapping FU-A")
                    self.fu_header = nal_header
                    self.fu = bytearray((nal_header,))
                if self.fu is None or self.fu_header != nal_header:
                    raise ValueError("FU-A continuation without matching start")
                if len(self.data) + len(self.fu) + len(payload) + 4 > MAX_ACCESS_UNIT:
                    raise ValueError("H.264 access unit exceeds 8 MiB")
                self.fu.extend(payload[2:])
                if end:
                    self._nal(bytes(self.fu))
                    self.fu = None
            else:
                raise ValueError("Unsupported H.264 packetization")
            if not packet.marker:
                return None
            if self.fu is not None:
                raise ValueError("Marker before FU-A completion")
            if not self.types.intersection((1, 2, 3, 4, 5)):
                self.data.clear()
                self.types.clear()
                return None
            if self.need_keyframe and 5 not in self.types:
                self.data.clear()
                self.types.clear()
                return None
            prefix = b"".join(
                START_CODE + self.parameters[k]
                for k in (7, 8)
                if k in self.parameters and k not in self.types
            )
            if len(prefix) + len(self.data) > MAX_ACCESS_UNIT:
                raise ValueError("H.264 access unit exceeds 8 MiB")
            result = prefix + bytes(self.data)
            self.data.clear()
            self.types.clear()
            self.need_keyframe = False
            return result
        except ValueError:
            self.loss()
            return None
