"""Tests for ThreadedTCPTransport."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time

import pytest

from siyi_sdk.exceptions import ConnectionError, NotConnectedError
from siyi_sdk.transport.threaded_tcp import ThreadedTCPTransport


class Peer:
    """A local TCP server that records what it receives and sends what a test asks of it."""

    def __init__(self) -> None:
        self.received = bytearray()
        self.data = asyncio.Event()  # set whenever something has been received
        self.eof = asyncio.Event()
        self.writer: asyncio.StreamWriter | None = None
        self.connected = asyncio.Event()
        self.on_connect: bytes = b""

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        try:
            if self.on_connect:
                writer.write(self.on_connect)
                await writer.drain()
            self.connected.set()
            while data := await reader.read(65536):
                self.received.extend(data)
                self.data.set()
        except ConnectionResetError:
            pass
        finally:
            self.eof.set()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


@pytest.fixture
async def peer() -> tuple[Peer, str, int]:
    state = Peer()
    server = await asyncio.start_server(state.handle, "127.0.0.1", 0)
    ip, port = server.sockets[0].getsockname()[:2]
    async with server:
        yield state, ip, port


async def test_connect_send_and_receive_with_arrival_stamps(peer: tuple[Peer, str, int]) -> None:
    state, ip, port = peer
    transport = ThreadedTCPTransport(ip, port)
    assert not transport.is_connected
    await transport.connect()
    assert transport.is_connected and transport.supports_heartbeat
    await transport.send(b"\x55\x66\x01")
    await asyncio.wait_for(state.data.wait(), 2)
    assert bytes(state.received) == b"\x55\x66\x01"
    sent = time.monotonic()
    assert state.writer is not None
    state.writer.write(b"reply")
    await state.writer.drain()
    stream = transport.stream_timed()
    chunk, arrived = await asyncio.wait_for(anext(stream), 2)
    assert chunk == b"reply"
    assert sent <= arrived <= time.monotonic()
    await transport.close()
    assert not transport.is_connected


async def test_chunks_are_stamped_when_they_arrive_not_when_they_are_read(
    peer: tuple[Peer, str, int],
) -> None:
    state, ip, port = peer
    state.on_connect = b"x" * 1000
    transport = ThreadedTCPTransport(ip, port)
    started = time.monotonic()
    await transport.connect()
    await asyncio.wait_for(state.connected.wait(), 2)
    await asyncio.sleep(0.4)  # the event loop is "busy": nothing consumes the data for a while
    chunk, arrived = await asyncio.wait_for(anext(transport.stream_timed()), 2)
    assert chunk == b"x" * 1000
    assert arrived - started < 0.3  # stamped near the connect, well before it was consumed
    await transport.close()


async def test_a_burst_is_read_in_pieces_larger_than_the_old_4_kib(
    peer: tuple[Peer, str, int],
) -> None:
    state, ip, port = peer
    burst = bytes(range(256)) * 1200  # 300 KB, like a burst of the camera's video
    transport = ThreadedTCPTransport(ip, port)
    await transport.connect()
    await asyncio.wait_for(state.connected.wait(), 2)
    assert state.writer is not None
    state.writer.write(burst)
    await state.writer.drain()
    sizes, received = [], bytearray()
    async for chunk in transport.stream():
        sizes.append(len(chunk))
        received.extend(chunk)
        if len(received) >= len(burst):
            break
    assert bytes(received) == burst
    assert max(sizes) > 4096
    await transport.close()


async def test_disables_nagle(peer: tuple[Peer, str, int]) -> None:
    _, ip, port = peer
    transport = ThreadedTCPTransport(ip, port)
    await transport.connect()
    assert transport.socket.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0
    await transport.close()


async def test_the_stream_ends_when_the_peer_closes(peer: tuple[Peer, str, int]) -> None:
    state, ip, port = peer
    transport = ThreadedTCPTransport(ip, port)
    await transport.connect()
    await asyncio.wait_for(state.connected.wait(), 2)
    assert state.writer is not None
    state.writer.write(b"last")
    state.writer.close()
    assert await asyncio.wait_for(_collect(transport), 2) == [b"last"]
    assert not transport.is_connected
    await transport.close()


async def test_close_stops_the_reader_thread_and_ends_the_stream(
    peer: tuple[Peer, str, int],
) -> None:
    _, ip, port = peer
    transport = ThreadedTCPTransport(ip, port)
    await transport.connect()
    reader = next(t for t in threading.enumerate() if t.name == "siyi-tcp-recv")
    consumer = asyncio.create_task(_collect(transport))
    await asyncio.sleep(0.05)
    await transport.close()
    assert await asyncio.wait_for(consumer, 2) == []  # woken by the close, not left hanging
    assert not reader.is_alive()
    with pytest.raises(NotConnectedError):
        await transport.send(b"x")


async def test_abort_resets_the_connection(peer: tuple[Peer, str, int]) -> None:
    state, ip, port = peer
    transport = ThreadedTCPTransport(ip, port)
    await transport.connect()
    await asyncio.wait_for(state.connected.wait(), 2)
    await transport.abort()
    assert not transport.is_connected
    await asyncio.wait_for(state.eof.wait(), 2)  # the peer sees the end (a reset or a close)


async def test_a_refused_connection_raises_connection_error() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens on this port any more
    transport = ThreadedTCPTransport("127.0.0.1", port, connect_timeout=0.5)
    with pytest.raises(ConnectionError):
        await transport.connect()
    assert not transport.is_connected


async def test_send_before_connect_raises() -> None:
    with pytest.raises(NotConnectedError, match="before connect"):
        await ThreadedTCPTransport("127.0.0.1", 12345).send(b"\x01")


async def test_connecting_twice_without_closing_is_an_error(peer: tuple[Peer, str, int]) -> None:
    _, ip, port = peer
    transport = ThreadedTCPTransport(ip, port)
    await transport.connect()
    with pytest.raises(RuntimeError, match="Close the previous"):
        await transport.connect()
    await transport.close()


async def _collect(transport: ThreadedTCPTransport) -> list[bytes]:
    return [chunk async for chunk in transport.stream()]
