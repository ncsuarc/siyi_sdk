"""The firmware client over a real TCP connection to a fake camera."""

from __future__ import annotations

import asyncio
import contextlib
import struct
import sys

from siyi_sdk.firmware_tracking import (
    FirmwareFrameParser,
    FirmwareTrackingClient,
    encode_frame,
    firmware_crc32,
)
from siyi_sdk.transport.threaded_tcp import ThreadedTCPTransport


def video_packet(index: int, size: int) -> bytes:
    """A 0x90 frame like the camera's: a 6-byte counter header, then an encoded picture."""
    payload = struct.pack("<IH", index, 1) + bytes([index % 251]) * size
    header = b"\x55\x66\xaa\xbb" + struct.pack("<BIHB", 2, len(payload), index, 0x90)
    packet = header + struct.pack("<I", firmware_crc32(header)) + payload
    return packet + struct.pack("<I", firmware_crc32(packet))


class FakeCamera:
    """Answers mode requests, and streams video in two bursts once 0x90 is switched on."""

    def __init__(self) -> None:
        self.mode = False
        self.commands: list[tuple[int, bytes]] = []
        self.gone = asyncio.Event()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        parser = FirmwareFrameParser()
        try:
            while data := await reader.read(65536):
                for frame in parser.feed(data):
                    self.commands.append((frame.command, frame.payload))
                    if frame.command == 0xA3:
                        self.mode = bool(frame.payload[0])
                    if frame.command in (0xA2, 0xA3):
                        reply = encode_frame(frame.command, bytes([self.mode]), 900, flags=2)
                        writer.write(reply)
                    if frame.command == 0x90 and frame.payload == b"\x01":
                        writer.write(b"".join(video_packet(n, 6000) for n in range(12)))
                        await writer.drain()
                        await asyncio.sleep(0.15)  # the pipe stalls, then delivers what queued
                        writer.write(b"".join(video_packet(n, 6000) for n in range(12, 20)))
                    await writer.drain()
        except ConnectionError:
            pass
        finally:
            self.gone.set()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def test_default_transport_reads_in_a_thread():
    client = FirmwareTrackingClient("127.0.0.1")
    assert isinstance(client._transport, ThreadedTCPTransport)


async def test_client_streams_bursty_video_and_ends_a_lock_like_the_ai_module():
    camera = FakeCamera()
    server = await asyncio.start_server(camera.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    events: list[tuple[str, dict]] = []
    video: list[tuple[int, int, float]] = []
    all_video = asyncio.Event()

    def on_video(index: int, data: bytes, arrival: float) -> None:
        video.append((index, len(data), arrival))
        if len(video) == 20:
            all_video.set()

    async with server:
        client = FirmwareTrackingClient(
            transport=ThreadedTCPTransport("127.0.0.1", port),
            trace=lambda event, fields: events.append((event, fields)),
            on_video=on_video,
        )
        await client.connect()
        connected = next(fields for event, fields in events if event == "connected")
        assert connected["quick_ack"] == (sys.platform == "win32")  # applied to the real socket

        await client.start_video()
        await asyncio.wait_for(all_video.wait(), 3)
        assert [index for index, _, _ in video] == list(range(20))
        assert all(size == 6000 for _, size, _ in video)
        arrivals = [arrival for _, _, arrival in video]
        assert arrivals == sorted(arrivals)
        assert arrivals[11] - arrivals[0] < 0.1  # the first burst came in together
        assert arrivals[12] - arrivals[11] > 0.1  # then the pipe stalled for a moment

        await client.set_mode(True)
        await client.send_target(640, 360, 60, 60)
        await client.cancel_target()
        await client.set_mode(False)
        await client.close()
        await asyncio.wait_for(camera.gone.wait(), 2)

    modes_and_targets = (0xA2, 0xA3, 0xAB)
    sent = [(c, payload) for c, payload in camera.commands if c in modes_and_targets]
    assert sent == [
        (0xA3, b"\x01"),
        (0xA2, b""),
        (0xAB, struct.pack("<HHHHBB", 640, 360, 60, 60, 255, 4)),
        (0xAB, struct.pack("<HHHHBB", 0, 0, 0, 0, 255, 3)),  # canceled, then AI off
        (0xA3, b"\x00"),
        (0xA2, b""),
    ]
    assert not camera.mode
