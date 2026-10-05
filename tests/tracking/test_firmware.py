"""Three focused checks for the recovered protocol and point-lock lifecycle."""

from __future__ import annotations

import asyncio
import struct
import threading
import time
from unittest.mock import AsyncMock

import numpy as np
import pytest

from siyi_sdk.firmware_tracking import (
    FirmwareFrameParser,
    FirmwareTrackingClient,
    encode_frame,
    firmware_crc32,
)
from siyi_sdk.tracking.firmware import FirmwarePointLock
from siyi_sdk.transport.mock import MockTransport


class Replies(MockTransport):
    """Only mode replies; target writes deliberately receive no success ACK."""

    mode = False
    silence = False
    drop_enable_reply = False

    async def send(self, data):
        await super().send(data)
        command, payload = data[11], data[16:-4]
        if command == 0xA3:
            self.mode = bool(payload[0])
            if self.mode and self.drop_enable_reply:
                return
        if not self.silence and command in (0xA2, 0xA3):
            # The camera allocates its own sequence number.
            self.queue_response(encode_frame(command, bytes([self.mode]), 900, flags=2))

    def targets(self):
        return [struct.unpack("<HHHHBB", p[16:-4]) for p in self.sent_frames if p[11] == 0xAB]


def test_firmware_vectors_and_tcp_parser():
    # Independently generated using cardv's table at 0xb94e0 and CRC32_cal at
    # 0x4f0ac; these are static-analysis vectors, not captured camera traffic.
    vectors = [
        (0xA2, b"", 1, "5566aabb01000000000100a224b603a4a04dda26"),
        (0xA3, b"\x01", 2, "5566aabb01010000000200a30b36fced01da2aa71d"),
        (
            0xAB,
            struct.pack("<HHHHBB", 640, 360, 60, 60, 255, 4),
            3,
            "5566aabb000a0000000300ab2b684d33800268013c003c00ff04d178d6ab",
        ),
        (0xA3, b"\x00", 4, "5566aabb01010000000400a319dd2fe900bd9e9011"),
    ]
    assert firmware_crc32(b"123456789") == 0x89A1897F
    packets = []
    for command, payload, seq, hex_packet in vectors:
        packet = bytes.fromhex(hex_packet)
        assert encode_frame(command, payload, seq, flags=0 if command == 0xAB else 1) == packet
        packets.append(packet)
    for split in range(len(packets[2]) + 1):
        parser = FirmwareFrameParser()
        frames = parser.feed(packets[2][:split]) + parser.feed(packets[2][split:] + packets[0])
        assert [(f.command, f.payload) for f in frames] == [(0xAB, vectors[2][1]), (0xA2, b"")]
    parser = FirmwareFrameParser()
    bad_header = bytearray(packets[0])
    bad_header[12] ^= 1
    bad_crc = packets[2][:-1] + bytes([packets[2][-1] ^ 1])
    oversized = b"\x55\x66\xaa\xbb" + struct.pack("<BIHB", 1, 0xFFFFFFFF, 1, 0xAB)
    oversized += struct.pack("<I", firmware_crc32(oversized))
    frames = parser.feed(b"junk" + bad_header + bad_crc + oversized + b"".join(packets))
    assert [f.sequence for f in frames] == [1, 2, 3, 4]


async def test_firmware_coordinates_loss_and_no_app_steering(monkeypatch):
    frame = np.random.default_rng(7).integers(0, 256, (360, 640, 3), dtype=np.uint8)
    for failure in ("confidence", "offscreen", "nonfinite", "resize"):
        transport = Replies()
        stop = AsyncMock()
        lock = FirmwarePointLock(FirmwareTrackingClient(transport=transport), stop=stop)
        await lock.start(frame, 320, 180, size=30)
        tracker = lock._tracker
        assert tracker is not None
        monkeypatch.setattr(tracker, "update", lambda _, tracker=tracker: (True, tracker.box()))
        monkeypatch.setattr(tracker, "center", lambda: (400, 90))
        await lock.update(frame, zoom=6.0)  # zoom must not be applied a second time
        assert transport.targets()[-1][:2] == (800, 180)
        assert transport.targets()[-1][4:] == (255, 4)
        assert transport.targets()[-1][2:4] == (62, 62)
        stop.assert_not_awaited()
        count = len(transport.targets())
        if failure == "confidence":
            monkeypatch.setattr(tracker, "getTrackingScore", lambda: 0.19)
        elif failure == "offscreen":
            monkeypatch.setattr(tracker, "center", lambda: (640, 180))
        elif failure == "nonfinite":
            monkeypatch.setattr(tracker, "center", lambda: (float("nan"), 180))
        await lock.update(frame[:200] if failure == "resize" else frame)
        assert not lock.active and not transport.mode and lock.exit_confirmed
        assert len(transport.targets()) == count
        assert not transport.is_connected
        assert all(p[11] in (0xA2, 0xA3, 0xAB) for p in transport.sent_frames)
        await lock.update(frame)
        assert len(transport.targets()) == count


async def test_firmware_activation_release_stale_and_disconnect(monkeypatch):
    from fastapi import HTTPException

    from web_ui import server

    frame = np.random.default_rng(9).integers(0, 256, (180, 320, 3), dtype=np.uint8)
    for outcome in (
        "release",
        "stale",
        "disconnect",
        "timeout",
        "busy",
        "activation_timeout",
        "unsupported",
        "inflight",
        "shutdown",
        "settings",
    ):
        transport = Replies()
        if outcome == "busy":
            transport.mode = True
        transport.drop_enable_reply = outcome == "activation_timeout"
        client = FirmwareTrackingClient(transport=transport)
        monkeypatch.setattr(server, "FirmwareTrackingClient", lambda _, client=client: client)
        state = server.CameraState()
        state.client = AsyncMock()
        state.ip = "192.168.144.25"
        state.is_connected = True
        state.firmware_version = server.FirmwareVersion(0x0307, 0x0409, 0)
        if outcome == "unsupported":
            state.firmware_version = server.FirmwareVersion(0x0306, 0x0408, 0)
        state.latest_image = frame
        state.latest_image_time = time.monotonic()
        state.pointing.lock_control = "firmware"
        monkeypatch.setattr(server, "state", state)
        if outcome == "unsupported":
            with pytest.raises(HTTPException, match="requires the inspected"):
                await state.start_lock(0, 0)
            assert not transport.sent_frames
            continue
        if outcome == "busy":
            with pytest.raises(HTTPException, match="already active"):
                await state.start_lock(0, 0)
            assert not transport.targets()
            assert all(p[11] == 0xA2 for p in transport.sent_frames)
            continue
        if outcome == "activation_timeout":
            with pytest.raises(HTTPException):
                await state.start_lock(0, 0)
            assert not transport.targets() and not transport.mode and not transport.is_connected
            assert state.lock_exit_confirmed
            continue
        snapshot = await state.start_lock(0, 0)
        assert snapshot["state"] == "locked" and snapshot["control"] == "firmware"
        assert snapshot["command"] is None and snapshot["error_deg"] is None
        lock = state.point_lock
        assert isinstance(lock, FirmwarePointLock)
        packets = transport.sent_frames
        assert [p[11] for p in packets[:4]] == [0xA2, 0xA3, 0xA2, 0xAB]
        if outcome == "stale":
            lock._last_frame -= 1
            await asyncio.sleep(0.07)
        elif outcome == "disconnect":
            transport.queue_error(ConnectionResetError("lost TCP connection"))
            await asyncio.sleep(0.07)
        elif outcome == "timeout":
            transport.silence = True
            with pytest.raises(HTTPException, match="exit is unconfirmed"):
                await state.release_lock()
        elif outcome == "shutdown":
            await state.shutdown()
        elif outcome == "settings":
            await server.set_pointing_config(
                server.PointingConfigRequest(**{**vars(state.pointing), "lock_control": "angle"})
            )
        elif outcome == "inflight":
            entered = asyncio.Event()
            resume = threading.Event()
            loop = asyncio.get_running_loop()
            tracker = lock._tracker
            assert tracker is not None

            def slow_update(_, tracker=tracker, loop=loop, entered=entered, resume=resume):
                loop.call_soon_threadsafe(entered.set)
                resume.wait(2)
                return True, tracker.box()

            monkeypatch.setattr(tracker, "update", slow_update)
            update = asyncio.create_task(lock.update(frame))
            await asyncio.wait_for(entered.wait(), 0.4)
            try:
                await state.release_lock()
            finally:
                resume.set()
            await update
        else:
            # Mode changes must disable tracking before sending the public command.
            async def capture(_, transport=transport):
                assert not transport.mode and not transport.is_connected

            state.client.capture.side_effect = capture
            monkeypatch.setattr(state, "begin_confirmation", lambda *_: None)
            await server.set_gimbal_mode(server.GimbalModeRequest(mode="LOCK"))
            state.client.capture.assert_awaited_once()
        await lock.release()  # concurrent/repeated release is idempotent
        assert not lock.active and not transport.is_connected
        if outcome in ("disconnect", "timeout"):
            assert not lock.exit_confirmed
            assert "unconfirmed" in state.lock_snapshot()["reason"]
            assert state.client.rotate_nowait.await_count == 2  # startup stop + fallback
            with pytest.raises(HTTPException):
                await state.start_lock(0, 0)  # no automatic fallback or restart
        else:
            assert lock.exit_confirmed and not transport.mode
        assert len(transport.targets()) == 1
