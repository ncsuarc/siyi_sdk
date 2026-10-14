"""A transport that behaves like an A8 Mini for the commands the camera service uses."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator

from siyi_sdk.constants import CTRL_ACK_PACK, CTRL_NEED_ACK
from siyi_sdk.protocol.frame import Frame
from siyi_sdk.protocol.parser import FrameParser
from siyi_sdk.transport.base import AbstractTransport

# DataStreamFreq value -> Hz
_FREQ_HZ = {0: 0, 1: 2, 2: 4, 3: 5, 4: 10, 5: 20, 6: 50, 7: 100}


class FakeCamera(AbstractTransport):
    """Answers queries, records motion commands and streams attitude when asked.

    ``alive = False`` makes it drop everything, like a powered-off camera.
    """

    def __init__(self, *, zoom: float = 1.0, camera_version: int = 0x00000308) -> None:
        self.alive = True
        self.camera_version = camera_version
        self.yaw = 0.0
        self.pitch = -10.0
        self.zoom = zoom
        self.rates: list[tuple[int, int]] = []
        self.angles: list[tuple[float, float]] = []
        self.zooms: list[int] = []
        self.received: list[int] = []
        self._connected = False
        self._parser = FrameParser()
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._stream_task: asyncio.Task[None] | None = None
        self._seq = 0

    # ------------------------------------------------------------------ transport

    async def connect(self) -> None:
        self._connected = True
        self._queue = asyncio.Queue()

    async def close(self) -> None:
        self._connected = False
        if self._stream_task is not None:
            self._stream_task.cancel()
            self._stream_task = None
        self._queue.put_nowait(None)

    async def send(self, data: bytes) -> None:
        for frame in self._parser.feed(data).frames:
            self.received.append(frame.cmd_id)
            if self.alive:
                self._handle(frame)

    async def stream(self) -> AsyncIterator[bytes]:  # type: ignore[override]
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            yield chunk

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def supports_heartbeat(self) -> bool:
        return False

    # ------------------------------------------------------------------ camera

    @property
    def last_rate(self) -> tuple[int, int] | None:
        return self.rates[-1] if self.rates else None

    def _reply(self, cmd_id: int, payload: bytes) -> None:
        self._seq = (self._seq + 1) & 0xFFFF
        frame = Frame(ctrl=CTRL_ACK_PACK, seq=self._seq, cmd_id=cmd_id, data=payload)
        self._queue.put_nowait(frame.to_bytes())

    def _attitude_payload(self) -> bytes:
        return struct.pack("<hhhhhh", round(self.yaw * 10), round(self.pitch * 10), 0, 0, 0, 0)

    def _handle(self, frame: Frame) -> None:
        cmd, data = frame.cmd_id, frame.data
        wants_reply = bool(frame.ctrl & CTRL_NEED_ACK)
        if cmd == 0x07:
            yaw, pitch = struct.unpack("<bb", data)
            self.rates.append((yaw, pitch))
            if wants_reply:
                self._reply(cmd, b"\x01")
        elif cmd == 0x0E:
            yaw10, pitch10 = struct.unpack("<hh", data)
            self.angles.append((yaw10 / 10, pitch10 / 10))
            self.yaw, self.pitch = yaw10 / 10, pitch10 / 10
            if wants_reply:
                self._reply(cmd, self._attitude_payload()[:6])
        elif cmd == 0x05:
            (direction,) = struct.unpack("<b", data)
            self.zooms.append(direction)
            if wants_reply:
                self._reply(cmd, struct.pack("<H", round(self.zoom * 10)))
        elif not wants_reply:
            return
        elif cmd == 0x01:
            self._reply(cmd, struct.pack("<III", self.camera_version, 0x00000409, 0))
        elif cmd == 0x0D:
            self._reply(cmd, self._attitude_payload())
        elif cmd == 0x08:
            self._reply(cmd, b"\x01")
        elif cmd == 0x0F:
            self.zoom = struct.unpack("<BB", data)[0] + struct.unpack("<BB", data)[1] / 10
            self._reply(cmd, b"\x01")
        elif cmd == 0x16:
            self._reply(cmd, struct.pack("<BB", 6, 0))
        elif cmd == 0x18:
            self._reply(cmd, struct.pack("<H", round(self.zoom * 10)))
        elif cmd == 0x25:
            data_type, freq = data[0], data[1]
            self._reply(cmd, bytes([data_type]))
            if data_type == 1:
                self._start_stream(_FREQ_HZ.get(freq, 0))

    def _start_stream(self, hz: float) -> None:
        if self._stream_task is not None:
            self._stream_task.cancel()
            self._stream_task = None
        if hz > 0:
            self._stream_task = asyncio.create_task(self._push_attitude(1.0 / hz))

    async def _push_attitude(self, period: float) -> None:
        while True:
            await asyncio.sleep(period)
            if self.alive and self._connected:
                frame = Frame(ctrl=CTRL_NEED_ACK, seq=0, cmd_id=0x0D, data=self._attitude_payload())
                self._queue.put_nowait(frame.to_bytes())
