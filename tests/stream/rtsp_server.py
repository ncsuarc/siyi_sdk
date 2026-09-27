"""Small local RTSP/RTP server for real TCP and UDP library integration tests."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import re
import struct
from pathlib import Path


def fixture_nals():
    wire = (Path(__file__).parents[1] / "fixtures/synthetic_red.h264").read_bytes()
    return [part for part in re.split(b"\x00\x00\x00?\x01", wire) if part]


class LocalRTSP:
    def __init__(self, packetization="fu", faults=False, stall=False):
        self.packetization = packetization
        self.faults = faults
        self.stall = stall
        self.server = None
        self.url = ""
        self.transports = []
        self.writers = set()
        self.tasks = set()
        self.setup_headers = []
        self.sessions = 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"rtsp://127.0.0.1:{port}/main.264"
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        for writer in list(self.writers):
            writer.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for transport in self.transports:
            transport.close()
        await self.server.wait_closed()

    def sdp(self):
        parameters = [nal for nal in fixture_nals() if nal[0] & 31 in (7, 8)]
        encoded = ",".join(base64.b64encode(nal).decode() for nal in parameters)
        return (
            "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Test\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
            f"a=fmtp:96 packetization-mode=1;sprop-parameter-sets={encoded}\r\n"
            "a=control:track1\r\na=range:npt=0-\r\n"
        ).encode()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        udp = None
        target = None
        producer = None
        try:
            while True:
                first = await reader.readexactly(1)
                if first == b"$":
                    header = await reader.readexactly(3)
                    await reader.readexactly(struct.unpack("!H", header[1:])[0])
                    continue
                request = first + await reader.readuntil(b"\r\n\r\n")
                lines = request.decode().split("\r\n")
                method = lines[0].split()[0]
                headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
                headers = {key.lower(): value.strip() for key, value in headers.items()}
                if int(headers.get("content-length", 0)):
                    await reader.readexactly(int(headers["content-length"]))
                response_headers = {"CSeq": headers["cseq"], "Session": "test;timeout=60"}
                content = b""
                if method == "OPTIONS":
                    response_headers["Public"] = (
                        "OPTIONS, DESCRIBE, SETUP, PLAY, GET_PARAMETER, TEARDOWN"
                    )
                elif method == "DESCRIBE":
                    content = self.sdp()
                    response_headers.update(
                        {"Content-Type": "application/sdp", "Content-Base": self.url + "/"}
                    )
                elif method == "SETUP":
                    transport_header = headers["transport"]
                    self.setup_headers.append(transport_header)
                    if "client_port" in transport_header:
                        port = int(re.search(r"client_port=(\d+)", transport_header)[1])
                        target = ("127.0.0.1", port)
                        udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                            asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
                        )
                        self.transports.append(udp)
                        server_port = udp.get_extra_info("sockname")[1]
                        transport_header += f";server_port={server_port}-{server_port}"
                    response_headers["Transport"] = transport_header
                elif method == "PLAY":
                    self.sessions += 1
                response_headers["Content-Length"] = str(len(content))
                wire = (
                    "RTSP/1.0 200 OK\r\n"
                    + "".join(f"{key}: {value}\r\n" for key, value in response_headers.items())
                    + "\r\n"
                )
                writer.write(wire.encode() + content)
                await writer.drain()
                if method == "PLAY":
                    producer = asyncio.create_task(self.produce(writer, udp, target))
                    self.tasks.add(producer)
                if method == "TEARDOWN":
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if producer is not None:
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
                self.tasks.discard(producer)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            self.writers.discard(writer)
            self.tasks.discard(task)

    async def produce(self, writer, udp, target):
        # SPS/PPS are advertised only in SDP. RTP carries the picture and SEI.
        nals = [nal for nal in fixture_nals() if nal[0] & 31 not in (7, 8)]
        seq = 65530
        frame_number = 0
        while True:
            timestamp = (frame_number * 3000) & 0xFFFFFFFF
            payloads = []
            if self.packetization == "stap":
                payloads = [b"\x78" + b"".join(struct.pack("!H", len(nal)) + nal for nal in nals)]
            else:
                for nal in nals:
                    if self.packetization == "single":
                        payloads.append(nal)
                    else:
                        chunk_size = min(40, max(1, (len(nal) - 1) // 2))
                        pieces = [
                            nal[offset : offset + chunk_size]
                            for offset in range(1, len(nal), chunk_size)
                        ]
                        for index, piece in enumerate(pieces):
                            flags = (0x80 if index == 0 else 0) | (
                                0x40 if index == len(pieces) - 1 else 0
                            )
                            payloads.append(
                                bytes(((nal[0] & 0xE0) | 28, (nal[0] & 31) | flags)) + piece
                            )
            packets = []
            for index, payload in enumerate(payloads):
                marker = index == len(payloads) - 1
                packets.append(
                    struct.pack("!BBHII", 0x80, 96 | (128 if marker else 0), seq, timestamp, 42)
                    + payload
                )
                seq = (seq + 1) & 65535
            if self.faults and frame_number == 0 and len(packets) > 3:
                packets.pop(2)  # First access unit is damaged; next IDR must recover.
            if self.faults and len(packets) > 3:
                packets[1], packets[2] = packets[2], packets[1]
                packets.insert(3, packets[2])  # A reordered duplicate.
            for packet in packets:
                if udp is not None:
                    udp.sendto(packet, target)
                else:
                    writer.write(b"$\x00" + struct.pack("!H", len(packet)) + packet)
                    await writer.drain()
                await asyncio.sleep(0)
            frame_number += 1
            if self.stall:
                await asyncio.Event().wait()
            await asyncio.sleep(1 / 30)
