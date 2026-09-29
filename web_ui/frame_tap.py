"""Transport wrapper that records every SDK frame for the dashboard's protocol log."""

from __future__ import annotations

import itertools
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from siyi_sdk import constants
from siyi_sdk.protocol.parser import FrameParser
from siyi_sdk.transport.base import AbstractTransport

CMD_NAMES: dict[int, str] = {
    value: name.removeprefix("CMD_")
    for name, value in vars(constants).items()
    if name.startswith("CMD_") and isinstance(value, int)
}
# Unsolicited frames the camera pushes; the log hides them by default.
PUSH_CMDS = frozenset({constants.CMD_REQUEST_GIMBAL_ATTITUDE, constants.CMD_FUNCTION_FEEDBACK})


class TapTransport(AbstractTransport):
    """Delegate to another transport, recording TX writes and decoded RX frames."""

    def __init__(self, inner: AbstractTransport, maxlen: int = 500) -> None:
        self.inner = inner
        self.records: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._ids = itertools.count(1)
        self._tx_parser = FrameParser()
        self._rx_parser = FrameParser()

    def _record(self, direction: str, raw: bytes, cmd_id: int | None = None, seq: int | None = None,
                error: str | None = None) -> None:
        self.records.append({
            "id": next(self._ids),
            "t": time.time(),
            "dir": direction,
            "cmd_id": cmd_id,
            "cmd": CMD_NAMES.get(cmd_id, f"0x{cmd_id:02X}") if cmd_id is not None else None,
            "seq": seq,
            "len": len(raw),
            "hex": raw.hex(" "),
            "push": cmd_id in PUSH_CMDS,
            "error": error,
        })

    def _log(self, direction: str, parser: FrameParser, chunk: bytes) -> None:
        parsed = parser.feed(chunk)
        for frame in parsed.frames:
            self._record(direction, frame.to_bytes(), frame.cmd_id, frame.seq)
        for error in parsed.errors:
            self._record("err", b"", error=f"{direction}: {error}")

    def since(self, record_id: int = 0) -> list[dict[str, Any]]:
        return [record for record in self.records if record["id"] > record_id]

    def between(self, start: float, end: float) -> list[dict[str, Any]]:
        return [record for record in self.records if start <= record["t"] <= end and not record["push"]]

    @property
    def last_id(self) -> int:
        return self.records[-1]["id"] if self.records else 0

    async def connect(self) -> None:
        self._tx_parser.reset()
        self._rx_parser.reset()
        await self.inner.connect()

    async def close(self) -> None:
        await self.inner.close()

    async def send(self, data: bytes) -> None:
        await self.inner.send(data)
        self._log("tx", self._tx_parser, data)

    async def stream(self) -> AsyncIterator[bytes]:
        async for chunk in self.inner.stream():
            self._log("rx", self._rx_parser, chunk)
            yield chunk

    @property
    def is_connected(self) -> bool:
        return self.inner.is_connected

    @property
    def supports_heartbeat(self) -> bool:
        return self.inner.supports_heartbeat
