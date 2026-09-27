"""Exercise serial cleanup and failure behavior without host serial hardware."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from siyi_sdk.exceptions import ConnectionError as SDKConnectionError
from siyi_sdk.exceptions import SendError
from siyi_sdk.transport.serial import SerialTransport


class Reader:
    def __init__(self, chunks: list[bytes | Exception]) -> None:
        self.chunks = iter(chunks)

    async def read(self, count: int) -> bytes:
        assert count == 4096
        item = next(self.chunks)
        if isinstance(item, Exception):
            raise item
        return item


class Writer:
    def __init__(self, *, send_error: bool = False, close_error: bool = False) -> None:
        self.sent: list[bytes] = []
        self.send_error = send_error
        self.close_error = close_error
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.send_error:
            raise OSError("port vanished")
        self.sent.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        if self.close_error:
            raise OSError("close failed")


async def test_serial_connect_send_receive_eof_and_close() -> None:
    reader, writer = Reader([b"reply", b""]), Writer()

    async def open_serial_connection(**kwargs):
        assert kwargs == {
            "url": "COM3",
            "baudrate": 57600,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
        }
        return reader, writer

    with patch.dict(
        sys.modules,
        {"serial_asyncio": SimpleNamespace(open_serial_connection=open_serial_connection)},
    ):
        transport = SerialTransport("COM3", baud=57600)
        assert not transport.supports_heartbeat
        await transport.connect()
        await transport.send(b"request")
        assert writer.sent == [b"request"]
        assert [chunk async for chunk in transport.stream()] == [b"reply"]
        assert not transport.is_connected
        await transport.close()
        await transport.close()
        assert writer.closed and transport._reader is None and transport._writer is None


async def test_serial_reader_reset_marks_disconnected() -> None:
    transport = SerialTransport("COM3")
    transport._reader = Reader([b"before", OSError("reset")])
    transport._connected = True
    assert [chunk async for chunk in transport.stream()] == [b"before"]
    assert not transport.is_connected


async def test_serial_send_failure_then_close_error_releases_references() -> None:
    transport = SerialTransport("COM3")
    writer = Writer(send_error=True, close_error=True)
    transport._writer = writer
    transport._reader = Reader([])
    transport._connected = True
    with pytest.raises(SendError, match="port vanished"):
        await transport.send(b"request")
    assert not transport.is_connected
    await transport.close()
    assert writer.closed and transport._reader is None and transport._writer is None


async def test_serial_open_failure_is_sdk_connection_error() -> None:
    async def open_serial_connection(**kwargs):
        raise OSError("missing port")

    with patch.dict(
        sys.modules,
        {"serial_asyncio": SimpleNamespace(open_serial_connection=open_serial_connection)},
    ):
        transport = SerialTransport("COM3")
        with pytest.raises(SDKConnectionError, match="missing port"):
            await transport.connect()
        assert not transport.is_connected
