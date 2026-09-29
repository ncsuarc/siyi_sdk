"""Regression coverage for supervised control and recoverable parsing."""

from __future__ import annotations

import asyncio
import random
import struct
from collections.abc import AsyncIterator

import pytest

from siyi_sdk.client import SIYIClient
from siyi_sdk.constants import HEARTBEAT_FRAME
from siyi_sdk.exceptions import ConnectionError, NotConnectedError
from siyi_sdk.models import DataStreamFreq, FCDataType, GimbalDataType
from siyi_sdk.protocol import Frame, FrameParser, crc16
from siyi_sdk.transport.mock import MockTransport


async def eventually(predicate, timeout=2.0):
    async def wait():
        while not predicate():  # noqa: ASYNC110 - intentional predicate polling
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), timeout)


class CameraMock(MockTransport):
    """Echo real request sequences while recording session ownership."""

    def __init__(self, heartbeat=False):
        super().__init__(supports_heartbeat=heartbeat)
        self.connections = 0
        self.readers = 0
        self.max_readers = 0
        self.fail_heartbeat = False
        self.fail_replay = False
        self.answer = True
        self.send_gate = None

    async def connect(self):
        await super().connect()
        self.connections += 1

    async def send(self, data):
        await super().send(data)
        if self.send_gate is not None:
            await self.send_gate.wait()
        if data == HEARTBEAT_FRAME:
            if self.fail_heartbeat:
                self.fail_heartbeat = False
                raise OSError("heartbeat reset")
            return
        request = Frame.from_bytes(data)
        if self.answer:
            payload = b"\x01" if request.cmd_id in (0x24, 0x25) else b"ok"
            if self.fail_replay and self.connections == 2 and request.cmd_id == 0x25:
                payload = b"\x00"
            self.queue_response(Frame(2, request.seq, request.cmd_id, payload).to_bytes())

    async def stream(self) -> AsyncIterator[bytes]:
        self.readers += 1
        self.max_readers = max(self.max_readers, self.readers)
        try:
            async for chunk in super().stream():
                yield chunk
        finally:
            self.readers -= 1


def test_mixed_corruption_at_every_two_split_boundaries():
    good = Frame(2, 65535, 1, b"payload").to_bytes()
    bad = good[:-1] + bytes((good[-1] ^ 1,))
    wire = good + bad + good
    for first in range(len(wire) + 1):
        for second in range(first, len(wire) + 1):
            parser = FrameParser()
            results = [
                parser.feed(part) for part in (wire[:first], wire[first:second], wire[second:])
            ]
            assert [frame.seq for result in results for frame in result.frames] == [65535, 65535]
            assert sum(len(result.errors) for result in results) == 1
            assert not parser.feed(b"").frames


def test_crc_random_initial_states_against_bitwise_reference():
    rng = random.Random(2026)
    for _ in range(256):
        data = rng.randbytes(rng.randrange(128))
        initial = rng.randrange(65536)
        expected = initial
        for byte in data:
            expected ^= byte << 8
            for _ in range(8):
                expected = ((expected << 1) ^ (0x1021 if expected & 0x8000 else 0)) & 65535
        assert crc16(data, initial) == expected


@pytest.mark.parametrize("failure", [StopAsyncIteration(), OSError("reset"), ValueError("reader")])
async def test_supervised_loss_fails_pending_and_reconnects(failure):
    transport = CameraMock(heartbeat=True)
    client = SIYIClient(transport, auto_reconnect=True)
    await client.connect()
    try:
        transport.answer = False
        request = asyncio.create_task(client._send_command(1, b""))
        await eventually(lambda: bool(client._pending))
        transport.queue_error(failure)
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(request, 0.2)
        assert not client.connection_event.is_set()
        with pytest.raises(NotConnectedError):
            await client._send_command(1, b"")
        transport.answer = True
        await asyncio.wait_for(client.connection_event.wait(), 2)
        assert transport.connections == 2
        assert transport.max_readers == 1
        assert client._heartbeat_task is not None and not client._heartbeat_task.done()
        assert not client._pending
    finally:
        await asyncio.gather(client.close(), client.close())
    assert not transport.is_connected and transport.readers == 0


async def test_heartbeat_uses_same_recovery():
    transport = CameraMock(heartbeat=True)
    transport.fail_heartbeat = True
    async with SIYIClient(transport, auto_reconnect=True) as client:
        await eventually(lambda: transport.connections == 2, 3)
        await client.connection_event.wait()
        assert transport.max_readers == 1


async def test_separate_registries_and_failed_replay_cleanup():
    transport = CameraMock()
    async with SIYIClient(transport, auto_reconnect=True, default_timeout=0.1) as client:
        await client.request_fc_stream(FCDataType.ATTITUDE, DataStreamFreq.HZ10)
        await client.request_gimbal_stream(GimbalDataType.ATTITUDE, DataStreamFreq.HZ5)
        assert len(client._fc_streams) == len(client._gimbal_streams) == 1
        transport.fail_replay = True
        transport.queue_error(StopAsyncIteration())
        await eventually(lambda: transport.connections == 3, 3)
        await asyncio.wait_for(client.connection_event.wait(), 1)
        replay = [
            Frame.from_bytes(data) for data in transport.sent_frames if data != HEARTBEAT_FRAME
        ]
        assert [(frame.cmd_id, frame.data) for frame in replay[-2:]] == [
            (0x24, bytes((FCDataType.ATTITUDE, DataStreamFreq.HZ10))),
            (0x25, bytes((GimbalDataType.ATTITUDE, DataStreamFreq.HZ5))),
        ]
        assert transport.max_readers == 1


@pytest.mark.parametrize("stage", ["send", "response", "retry", "lock"])
async def test_cancellation_cleans_only_owned_pending_future(stage):
    transport = CameraMock()
    transport.answer = False
    client = SIYIClient(
        transport, default_timeout=0.01 if stage == "retry" else 1.0, retry_base_delay=0.2
    )
    await client.connect()
    try:
        if stage == "send":
            transport.send_gate = asyncio.Event()
        request = asyncio.create_task(client._send_command(1, b""))
        await eventually(lambda: bool(client._pending))
        if stage == "retry":
            await eventually(lambda: not client._pending)
        if stage == "lock":
            owned = client._pending[1]
            waiter = asyncio.create_task(client._send_command(1, b""))
            await asyncio.sleep(0)
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            assert client._pending[1] is owned
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        assert not client._pending and not client._pending_meta
    finally:
        await client.close()


async def test_sequence_retry_wrap_and_delayed_duplicate():
    transport = CameraMock()
    transport.answer = False
    async with SIYIClient(
        transport, default_timeout=0.05, retry_base_delay=0, response_matching="sequence"
    ) as client:
        client._seq = 65535
        query = asyncio.create_task(client._send_command(1, b""))
        await eventually(lambda: len(transport.sent_frames) >= 2)
        transport.queue_response(Frame(0, 65535, 1, b"stale").to_bytes())
        await asyncio.sleep(0)
        transport.queue_response(Frame(0, 0, 1, b"fresh").to_bytes())
        assert await query == b"fresh"
        assert [Frame.from_bytes(data).seq for data in transport.sent_frames] == [65535, 0]


@pytest.mark.parametrize(
    "cmd,payload",
    [
        (0x20, b"\x01"),
        (0x21, b"\x01"),
        (0x24, b"\x01\x0a"),
        (0x25, b"\x01\x05"),
        (0x49, b"\x00"),
        (0x4A, b"\x00\x02"),
    ],
)
async def test_wrong_echo_selector_is_rejected(cmd, payload):
    transport = CameraMock()
    transport.answer = False
    async with SIYIClient(transport, max_retries=0) as client:
        query = asyncio.create_task(client._send_command(cmd, payload))
        await eventually(lambda: bool(client._pending))
        transport.queue_response(Frame(2, 0, cmd, bytes((payload[0] ^ 1,)) + b"\x01").to_bytes())
        await asyncio.sleep(0.005)
        assert not query.done()
        transport.queue_response(Frame(0, 0, cmd, payload[:1] + b"\x01").to_bytes())
        assert await query == payload[:1] + b"\x01"


async def test_attitude_query_also_delivers_to_snapshot_subscribers():
    transport = CameraMock()
    transport.answer = False
    async with SIYIClient(transport) as client:
        received = []

        def broken(attitude):
            unsubscribe()
            raise RuntimeError("callback")

        unsubscribe = client.on_attitude(broken)
        client.on_attitude(received.append)
        query = asyncio.create_task(client.get_gimbal_attitude())
        await eventually(lambda: bool(client._pending))
        transport.queue_response(
            Frame(0, 0, 0x0D, struct.pack("<hhhhhh", 10, 20, 0, 0, 0, 0)).to_bytes()
        )
        attitude = await query
        assert received == [attitude]
        assert not client._pending


async def test_close_during_reconnect_and_cancelled_close_still_releases_resources():
    transport = CameraMock()
    client = SIYIClient(transport, auto_reconnect=True)
    await client.connect()
    transport.queue_error(OSError("lost"))
    await eventually(lambda: not client.connection_event.is_set())
    close = asyncio.create_task(client.close())
    await asyncio.sleep(0)
    close.cancel()
    await asyncio.gather(close, return_exceptions=True)
    await client.close()
    assert not client.connection_event.is_set()
    assert not transport.is_connected
    assert client._reader_task is client._heartbeat_task is client._supervisor_task is None
