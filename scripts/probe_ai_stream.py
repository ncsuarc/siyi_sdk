"""Capture the A8 Mini's private video stream (port 37256, command 0x90) for analysis.

This is the stream the SIYI AI module tracks on. The script does not enable AI
mode or move the gimbal: it requests the stream, records it for a few seconds,
stops it, and reports the packet layout, frame rate and codec.

    python scripts/probe_ai_stream.py [seconds] [ip]
"""

from __future__ import annotations

import asyncio
import collections
import struct
import sys
import time
from pathlib import Path

from siyi_sdk.firmware_tracking import encode_frame, firmware_crc32

MAGIC = b"\x55\x66\xaa\xbb"
MAX_PAYLOAD = 0x60000  # whole encoded frames, up to ~358 KB in cardv


def parse(buffer: bytearray) -> list[tuple[int, int, int, bytes]]:
    """Return complete (command, flags, sequence, payload) packets; keep the rest buffered."""
    frames = []
    while True:
        start = buffer.find(MAGIC)
        if start < 0:
            del buffer[:-3]
            return frames
        del buffer[:start]
        if len(buffer) < 16:
            return frames
        flags, length, sequence, command = struct.unpack_from("<BIHB", buffer, 4)
        if (flags > 3 or length > MAX_PAYLOAD
                or struct.unpack_from("<I", buffer, 12)[0] != firmware_crc32(bytes(buffer[:12]))):
            del buffer[0]
            continue
        end = 16 + length
        if len(buffer) < end + 4:
            return frames
        if struct.unpack_from("<I", buffer, end)[0] != firmware_crc32(bytes(buffer[:end])):
            del buffer[0]
            continue
        frames.append((command, flags, sequence, bytes(buffer[16:end])))
        del buffer[: end + 4]


def nal_types(data: bytes) -> tuple[collections.Counter, collections.Counter]:
    """Count NAL unit header bytes after Annex-B start codes, as H.264 and as H.265 types."""
    h264, h265 = collections.Counter(), collections.Counter()
    i = data.find(b"\x00\x00\x01")
    while 0 <= i < len(data) - 3:
        header = data[i + 3]
        h264[header & 0x1F] += 1
        h265[(header >> 1) & 0x3F] += 1
        i = data.find(b"\x00\x00\x01", i + 3)
    return h264, h265


async def main(seconds: float, ip: str) -> None:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, 37256), 1.5)
    sequence = 0

    async def send(command: int, payload: bytes) -> None:
        nonlocal sequence
        sequence += 1
        writer.write(encode_frame(command, payload, sequence, flags=1))
        await writer.drain()

    async def keepalive() -> None:
        while True:
            await asyncio.sleep(1.0)
            await send(0x80, b"\x01")

    buffer = bytearray()
    video: list[tuple[float, int, bytes, bytes]] = []  # arrival, flags, header, data
    other = collections.Counter()
    started = time.monotonic()
    tick = asyncio.create_task(keepalive())
    await send(0x90, b"\x01")
    try:
        while time.monotonic() - started < seconds:
            try:
                chunk = await asyncio.wait_for(reader.read(1 << 16), 0.5)
            except TimeoutError:
                continue
            if not chunk:
                print("camera closed the connection")
                break
            now = time.monotonic() - started
            buffer.extend(chunk)
            for command, flags, _, payload in parse(buffer):
                if command == 0x90 and len(payload) > 6:
                    video.append((now, flags, payload[:6], payload[6:]))
                else:
                    other[(f"0x{command:02X}", payload[:8].hex(" "))] += 1
    finally:
        await send(0x90, b"\x00")
        await asyncio.sleep(0.3)
        tick.cancel()
        writer.transport.abort()

    print(f"other packets: {dict(other)}")
    if not video:
        print("no video packets received")
        return
    span = video[-1][0] - video[0][0]
    sizes = sorted(len(v[3]) for v in video)
    print(f"video packets: {len(video)} in {span:.2f} s = {len(video) / max(span, 1e-9):.1f}/s; "
          f"first after {video[0][0] * 1000:.0f} ms")
    print(f"frame bytes min/median/max: {sizes[0]}/{sizes[len(sizes) // 2]}/{sizes[-1]}")
    print("first headers (flags, 6-byte header):")
    for _, flags, header, _ in video[:8]:
        print(f"  flags={flags} header={header.hex(' ')} u32le={struct.unpack('<I', header[:4])[0]}")
    gaps = [b[0] - a[0] for a, b in zip(video, video[1:])]
    if gaps:
        print(f"arrival gaps ms min/median/max: {min(gaps) * 1000:.0f}/"
              f"{sorted(gaps)[len(gaps) // 2] * 1000:.0f}/{max(gaps) * 1000:.0f}")
    stream = b"".join(v[3] for v in video)
    h264, h265 = nal_types(stream)
    print(f"NAL types if H.264: {dict(h264.most_common(8))}")
    print(f"NAL types if H.265: {dict(h265.most_common(8))}")
    out = Path("ai_stream_capture.bin")
    out.write_bytes(stream)
    print(f"saved {len(stream)} bytes to {out.resolve()}")
    try:
        import av
    except ImportError:
        print("PyAV not installed; skipping decode")
        return
    for codec in ("h264", "hevc"):
        context = av.CodecContext.create(codec, "r")
        decoded = []
        for _, _, _, data in video:
            try:
                for packet in context.parse(data):
                    decoded.extend(context.decode(packet))
            except av.error.FFmpegError:
                pass
        if decoded:
            frame = decoded[-1]
            print(f"decodes as {codec}: {len(decoded)} frames, {frame.width}x{frame.height}")
            return
    print("did not decode as H.264 or H.265")


if __name__ == "__main__":
    asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 5.0,
                     sys.argv[2] if len(sys.argv) > 2 else "192.168.144.25"))
