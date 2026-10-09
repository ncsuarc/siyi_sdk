# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for the SIYIClient high-level API."""

from __future__ import annotations

import asyncio
import struct
from ipaddress import IPv4Address

import pytest

from siyi_sdk.client import SIYIClient
from siyi_sdk.constants import (
    CMD_CAPTURE_PHOTO_RECORD_VIDEO,
    CMD_REQUEST_FIRMWARE_VERSION,
    CMD_REQUEST_GIMBAL_ATTITUDE,
    CMD_SEND_AIRCRAFT_ATTITUDE,
    CMD_SEND_RAW_GPS,
)
from siyi_sdk.exceptions import (
    ConfigurationError,
    ResponseError,
    TimeoutError,
    UnsupportedCommandError,
)
from siyi_sdk.models import (
    AircraftAttitude,
    CameraSystemInfo,
    CaptureFuncType,
    CenteringAction,
    DataStreamFreq,
    EncodingParams,
    FCDataType,
    FileNameType,
    FileType,
    FirmwareVersion,
    FunctionFeedback,
    GimbalAttitude,
    GimbalDataType,
    GimbalMotionMode,
    HardwareID,
    IPConfig,
    MagneticEncoderAngles,
    RawGPS,
    SetAttitudeAck,
    StreamType,
    VideoEncType,
    WeakControlThreshold,
    ZoomRange,
)
from siyi_sdk.protocol.frame import Frame
from siyi_sdk.transport.mock import MockTransport


@pytest.fixture
def mock_transport() -> MockTransport:
    """Create a mock transport for testing."""
    return MockTransport(supports_heartbeat=False)


@pytest.fixture
def mock_transport_tcp() -> MockTransport:
    """Create a mock transport simulating TCP with heartbeat support."""
    return MockTransport(supports_heartbeat=True)


async def _reply_in_order(transport: MockTransport, replies: list[Frame]) -> None:
    """Queue each reply only after the matching request has been sent."""
    for sent, reply in enumerate(replies, 1):
        while len(transport.sent_frames) < sent:  # noqa: ASYNC110 - mock exposes no send event
            await asyncio.sleep(0.001)
        transport.queue_response(reply.to_bytes())


class TestClientLifecycle:
    """Test client lifecycle management."""

    @pytest.mark.asyncio
    async def test_context_manager(self, mock_transport: MockTransport) -> None:
        """Test async context manager usage."""
        async with SIYIClient(mock_transport) as client:
            assert mock_transport.is_connected
            assert client._reader_task is not None

        # After exit, transport should be closed
        assert not mock_transport.is_connected

    @pytest.mark.asyncio
    async def test_manual_connect_close(self, mock_transport: MockTransport) -> None:
        """Test manual connect/close lifecycle."""
        client = SIYIClient(mock_transport)
        assert not mock_transport.is_connected

        await client.connect()
        assert mock_transport.is_connected

        await client.close()
        assert not mock_transport.is_connected

    @pytest.mark.asyncio
    async def test_heartbeat_started_for_tcp(self, mock_transport_tcp: MockTransport) -> None:
        """Test heartbeat task is started for TCP transports."""
        client = SIYIClient(mock_transport_tcp)
        await client.connect()

        assert client._heartbeat_task is not None
        assert not client._heartbeat_task.done()

        await client.close()

    @pytest.mark.asyncio
    async def test_heartbeat_not_started_for_udp(self, mock_transport: MockTransport) -> None:
        """Test heartbeat task is not started for UDP transports."""
        client = SIYIClient(mock_transport)
        await client.connect()

        assert client._heartbeat_task is None

        await client.close()

    @pytest.mark.asyncio
    async def test_heartbeat_sends_frames(self, mock_transport_tcp: MockTransport) -> None:
        """Test heartbeat task sends 3 frames in 3.1 seconds."""
        client = SIYIClient(mock_transport_tcp)
        await client.connect()

        # Wait for 3.1 seconds
        await asyncio.sleep(3.1)

        await client.close()

        # Check sent frames for heartbeat frames
        heartbeat_count = sum(
            1 for frame in mock_transport_tcp.sent_frames if frame.hex() == "556601010000000000598b"
        )
        assert heartbeat_count == 3

    @pytest.mark.asyncio
    async def test_no_heartbeat_for_udp(self, mock_transport: MockTransport) -> None:
        """Test no heartbeat frames for UDP transport."""
        client = SIYIClient(mock_transport)
        await client.connect()

        await asyncio.sleep(3.1)

        await client.close()

        # No heartbeat frames should be sent
        heartbeat_count = sum(
            1 for frame in mock_transport.sent_frames if frame.hex() == "556601010000000000598b"
        )
        assert heartbeat_count == 0


class TestSequenceNumber:
    """Test sequence number generation."""

    @pytest.mark.asyncio
    async def test_seq_increment(self, mock_transport: MockTransport) -> None:
        """Test sequence numbers increment correctly."""
        client = SIYIClient(mock_transport)

        seq1 = client._next_seq()
        seq2 = client._next_seq()
        seq3 = client._next_seq()

        assert seq1 == 0
        assert seq2 == 1
        assert seq3 == 2

    @pytest.mark.asyncio
    async def test_seq_wrap(self, mock_transport: MockTransport) -> None:
        """Test sequence number wraps at 0xFFFF."""
        client = SIYIClient(mock_transport)
        client._seq = 0xFFFE

        seq1 = client._next_seq()
        seq2 = client._next_seq()
        seq3 = client._next_seq()

        assert seq1 == 0xFFFE
        assert seq2 == 0xFFFF
        assert seq3 == 0x0000

    @pytest.mark.asyncio
    async def test_seq_uniqueness_70k(self, mock_transport: MockTransport) -> None:
        """Test 70,000 sequence numbers cover all 16-bit values."""
        client = SIYIClient(mock_transport)

        seqs = [client._next_seq() for _ in range(70000)]

        # All 16-bit values should be produced
        unique_seqs = {s & 0xFFFF for s in seqs}
        assert len(unique_seqs) == 65536


class TestCommandExecution:
    """Test command execution and timeout handling."""

    @pytest.mark.asyncio
    async def test_get_firmware_version(self, mock_transport: MockTransport) -> None:
        """Test get_firmware_version happy path."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK response
        ack_payload = b"\x03\x02\x02\x6e\x03\x02\x02\x6e\x01\x01\x01\x63"
        ack_frame = Frame.build(CMD_REQUEST_FIRMWARE_VERSION, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        version = await client.get_firmware_version()

        assert isinstance(version, FirmwareVersion)
        assert version.camera == 0x6E020203
        assert version.gimbal == 0x6E020203

        await client.close()

    @pytest.mark.asyncio
    async def test_timeout_raises_error(self, mock_transport: MockTransport) -> None:
        """Test timeout raises TimeoutError."""
        client = SIYIClient(mock_transport, default_timeout=0.1)
        await client.connect()

        # Do not queue a response
        with pytest.raises(TimeoutError) as exc_info:
            await client.get_firmware_version()

        err = exc_info.value
        assert err.cmd_id == CMD_REQUEST_FIRMWARE_VERSION
        assert err.timeout_s == 0.1

        await client.close()

    @pytest.mark.asyncio
    async def test_retry_on_idempotent_read(self, mock_transport: MockTransport) -> None:
        """Test idempotent reads retry on timeout."""
        client = SIYIClient(mock_transport, default_timeout=0.1, max_retries=1)
        await client.connect()

        # Queue response after both attempts would have been sent
        async def delayed_response() -> None:
            await asyncio.sleep(
                0.25
            )  # Wait for first timeout + retry delay + part of second attempt
            ack_payload = b"\x03\x02\x02\x6e\x03\x02\x02\x6e\x01\x01\x01\x63"
            ack_frame = Frame.build(
                CMD_REQUEST_FIRMWARE_VERSION, ack_payload, seq=0, need_ack=False
            )
            mock_transport.queue_response(ack_frame.to_bytes())

        task = asyncio.create_task(delayed_response())

        version = await client.get_firmware_version()
        await task
        assert isinstance(version, FirmwareVersion)

        await client.close()

    @pytest.mark.asyncio
    async def test_no_retry_on_write(self, mock_transport: MockTransport) -> None:
        """Test write commands do not retry on timeout."""
        client = SIYIClient(mock_transport, default_timeout=0.1, max_retries=1)
        await client.connect()

        # Do not queue response
        with pytest.raises(TimeoutError):
            await client.set_osd_flag(True)
        assert len(mock_transport.sent_frames) == 1

        await client.close()

    @pytest.mark.asyncio
    async def test_angle_setpoints_are_not_retried(self, mock_transport: MockTransport) -> None:
        """A late retry would drive the gimbal to a stale target."""
        client = SIYIClient(mock_transport, default_timeout=0.05, max_retries=3)
        await client.connect()

        with pytest.raises(TimeoutError):
            await client.set_attitude(10.0, -20.0)
        with pytest.raises(TimeoutError):
            await client.set_single_axis("pitch", -20.0)
        assert len(mock_transport.sent_frames) == 2

        await client.close()

    @pytest.mark.asyncio
    async def test_retry_backoff_is_capped(
        self, mock_transport: MockTransport, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exponential backoff never sleeps longer than one second."""
        client = SIYIClient(mock_transport, default_timeout=0.01, max_retries=8)
        await client.connect()
        delays: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            await real_sleep(0)

        monkeypatch.setattr("siyi_sdk.client.asyncio.sleep", fake_sleep)
        with pytest.raises(TimeoutError):
            await client.get_firmware_version()
        monkeypatch.undo()

        assert delays and max(delays) == 1.0

        await client.close()

    @pytest.mark.asyncio
    async def test_concurrent_same_cmd_id(self, mock_transport: MockTransport) -> None:
        """Test concurrent requests with same CMD_ID are serialized."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue responses with a delay to ensure requests are sent first
        async def queue_responses() -> None:
            await asyncio.sleep(0.05)
            ack_payload1 = b"\x03\x02\x02\x6e\x03\x02\x02\x6e\x01\x01\x01\x63"
            ack_frame1 = Frame.build(
                CMD_REQUEST_FIRMWARE_VERSION, ack_payload1, seq=0, need_ack=False
            )
            mock_transport.queue_response(ack_frame1.to_bytes())

            await asyncio.sleep(0.05)
            ack_payload2 = b"\x04\x03\x03\x6f\x04\x03\x03\x6f\x02\x02\x02\x64"
            ack_frame2 = Frame.build(
                CMD_REQUEST_FIRMWARE_VERSION, ack_payload2, seq=1, need_ack=False
            )
            mock_transport.queue_response(ack_frame2.to_bytes())

        task = asyncio.create_task(queue_responses())

        # Execute concurrently
        results = await asyncio.gather(client.get_firmware_version(), client.get_firmware_version())
        await task

        # Both should succeed
        assert len(results) == 2
        assert all(isinstance(r, FirmwareVersion) for r in results)

        # Check sent frames (should be 2)
        assert len(mock_transport.sent_frames) == 2

        await client.close()

    @pytest.mark.asyncio
    async def test_concurrent_different_cmd_id(self, mock_transport: MockTransport) -> None:
        """Test concurrent requests with different CMD_IDs execute in parallel."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue responses with a delay
        async def queue_responses() -> None:
            await asyncio.sleep(0.05)
            firmware_ack = b"\x03\x02\x02\x6e\x03\x02\x02\x6e\x01\x01\x01\x63"
            firmware_frame = Frame.build(
                CMD_REQUEST_FIRMWARE_VERSION, firmware_ack, seq=0, need_ack=False
            )
            mock_transport.queue_response(firmware_frame.to_bytes())

            attitude_ack = b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            attitude_frame = Frame.build(
                CMD_REQUEST_GIMBAL_ATTITUDE, attitude_ack, seq=1, need_ack=False
            )
            mock_transport.queue_response(attitude_frame.to_bytes())

        response_task = asyncio.create_task(queue_responses())

        # Execute concurrently
        firmware_task = asyncio.create_task(client.get_firmware_version())
        attitude_task = asyncio.create_task(client.get_gimbal_attitude())

        firmware, attitude = await asyncio.gather(firmware_task, attitude_task)
        await response_task

        assert isinstance(firmware, FirmwareVersion)
        assert isinstance(attitude, GimbalAttitude)

        await client.close()

    @pytest.mark.asyncio
    async def test_fire_and_forget_capture(self, mock_transport: MockTransport) -> None:
        """Test fire-and-forget commands do not wait for ACK."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Do not queue response
        await client.capture(CaptureFuncType.PHOTO)

        # Should return immediately
        assert len(mock_transport.sent_frames) == 1
        sent_frame = Frame.from_bytes(mock_transport.sent_frames[0])
        assert sent_frame.cmd_id == CMD_CAPTURE_PHOTO_RECORD_VIDEO

        await client.close()

    @pytest.mark.asyncio
    async def test_fire_and_forget_send_aircraft_attitude(
        self, mock_transport: MockTransport
    ) -> None:
        """Test send_aircraft_attitude is fire-and-forget."""
        client = SIYIClient(mock_transport)
        await client.connect()

        att = AircraftAttitude(
            time_boot_ms=1000,
            roll_rad=0.1,
            pitch_rad=0.2,
            yaw_rad=0.3,
            rollspeed=0.01,
            pitchspeed=0.02,
            yawspeed=0.03,
        )

        await client.send_aircraft_attitude(att)

        sent_frame = Frame.from_bytes(mock_transport.sent_frames[0])
        assert sent_frame.cmd_id == CMD_SEND_AIRCRAFT_ATTITUDE

        await client.close()

    @pytest.mark.asyncio
    async def test_fire_and_forget_send_raw_gps(self, mock_transport: MockTransport) -> None:
        """Test send_raw_gps is fire-and-forget."""
        client = SIYIClient(mock_transport)
        await client.connect()

        gps = RawGPS(
            time_boot_ms=2000,
            lat_e7=123456789,
            lon_e7=987654321,
            alt_msl_cm=100000,
            alt_ellipsoid_cm=100500,
            vn_mmps=1000,
            ve_mmps=2000,
            vd_mmps=500,
        )

        await client.send_raw_gps(gps)

        sent_frame = Frame.from_bytes(mock_transport.sent_frames[0])
        assert sent_frame.cmd_id == CMD_SEND_RAW_GPS

        await client.close()


class TestStreamSubscriptions:
    """Test stream subscription API."""

    @pytest.mark.asyncio
    async def test_on_attitude_subscription(self, mock_transport: MockTransport) -> None:
        """Test attitude stream subscription."""
        client = SIYIClient(mock_transport)
        await client.connect()

        received: list[GimbalAttitude] = []

        def callback(att: GimbalAttitude) -> None:
            received.append(att)

        unsub = client.on_attitude(callback)

        # Queue 5 attitude push frames
        for i in range(5):
            payload = b"\x00\x00" * 6  # 12 bytes of zeros
            frame = Frame.build(CMD_REQUEST_GIMBAL_ATTITUDE, payload, seq=i, need_ack=False)
            mock_transport.queue_response(frame.to_bytes())

        # Allow reader to process
        await asyncio.sleep(0.2)

        assert len(received) == 5

        # Unsubscribe
        unsub()

        # Queue another frame
        frame = Frame.build(CMD_REQUEST_GIMBAL_ATTITUDE, b"\x00\x00" * 6, seq=10, need_ack=False)
        mock_transport.queue_response(frame.to_bytes())

        await asyncio.sleep(0.2)

        # Should still be 5 (no new callbacks)
        assert len(received) == 5

        await client.close()

    @pytest.mark.asyncio
    async def test_on_function_feedback_subscription(self, mock_transport: MockTransport) -> None:
        """Test function feedback stream subscription."""
        client = SIYIClient(mock_transport)
        await client.connect()

        received: list[FunctionFeedback] = []

        def callback(fb: FunctionFeedback) -> None:
            received.append(fb)

        unsub = client.on_function_feedback(callback)

        # Queue 2 function feedback frames
        for i in range(2):
            payload = b"\x00"  # PHOTO_OK
            frame = Frame.build(0x0B, payload, seq=i, need_ack=False)
            mock_transport.queue_response(frame.to_bytes())

        await asyncio.sleep(0.2)

        assert len(received) == 2
        assert all(fb == FunctionFeedback.PHOTO_OK for fb in received)

        unsub()
        await client.close()


class TestUnexpectedFrames:
    """Test handling of unexpected frames."""

    @pytest.mark.asyncio
    async def test_unknown_cmd_id_logged(
        self, mock_transport: MockTransport, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Test unknown CMD_ID is logged as warning."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue a frame with unknown CMD_ID
        frame = Frame.build(0x03, b"", seq=0, need_ack=False)
        mock_transport.queue_response(frame.to_bytes())

        await asyncio.sleep(0.2)

        # Check stdout (structlog outputs to stdout by default)
        captured = capsys.readouterr()
        assert "unexpected_frame" in captured.out or "0x03" in captured.out

        await client.close()


class TestSystemCommands:
    """Test all system command methods."""

    @pytest.mark.asyncio
    async def test_get_hardware_id(self, mock_transport: MockTransport) -> None:
        """Test get_hardware_id."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK
        ack_payload = b"\x73" + b"\x00" * 11  # A8_MINI product ID
        ack_frame = Frame.build(0x02, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        hw_id = await client.get_hardware_id()

        assert isinstance(hw_id, HardwareID)
        assert hw_id.raw[0] == 0x73

        await client.close()

    @pytest.mark.asyncio
    async def test_set_utc_time(self, mock_transport: MockTransport) -> None:
        """Test set_utc_time."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK
        ack_payload = b"\x01"  # Success
        ack_frame = Frame.build(0x30, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        result = await client.set_utc_time(1234567890123456)

        assert result is True

        await client.close()

    @pytest.mark.asyncio
    async def test_soft_reboot(self, mock_transport: MockTransport) -> None:
        """Test soft_reboot."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK
        ack_payload = b"\x01\x01"  # Both rebooted
        ack_frame = Frame.build(0x80, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        camera, gimbal = await client.soft_reboot(camera=True, gimbal=True)

        assert camera is True
        assert gimbal is True

        await client.close()


class TestDigitalZoomCommands:
    """Test digital zoom commands."""

    @pytest.mark.asyncio
    async def test_manual_zoom(self, mock_transport: MockTransport) -> None:
        """Test manual_zoom."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK (2 bytes: zoom = 5.3x)
        ack_payload = b"\x35\x00"  # 53 in little-endian
        ack_frame = Frame.build(0x05, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        zoom = await client.manual_zoom(1)

        assert zoom == 5.3

        await client.close()

    @pytest.mark.asyncio
    async def test_absolute_zoom(self, mock_transport: MockTransport) -> None:
        """Test absolute_zoom."""
        client = SIYIClient(mock_transport)
        await client.connect()

        replies = asyncio.create_task(
            _reply_in_order(
                mock_transport,
                [Frame.build(0x16, b"\x05\x05", seq=0), Frame.build(0x0F, b"\x01", seq=0)],
            )
        )

        await client.absolute_zoom(5.5)
        await replies
        assert [Frame.from_bytes(f).cmd_id for f in mock_transport.sent_frames] == [0x16, 0x0F]

        await client.close()

    @pytest.mark.asyncio
    async def test_absolute_zoom_rejects_above_resolution_max(
        self, mock_transport: MockTransport
    ) -> None:
        """A 4K recording caps zoom at 1.0x, where the camera never ACKs 0x0F."""
        client = SIYIClient(mock_transport)
        await client.connect()
        replies = asyncio.create_task(
            _reply_in_order(mock_transport, [Frame.build(0x16, b"\x01\x00", seq=0)])
        )

        with pytest.raises(ConfigurationError):
            await client.absolute_zoom(2.0)
        await replies
        assert [Frame.from_bytes(f).cmd_id for f in mock_transport.sent_frames] == [0x16]

        await client.close()

    @pytest.mark.asyncio
    async def test_get_zoom_range(self, mock_transport: MockTransport) -> None:
        """Test get_zoom_range."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK (2 bytes: max = 30.5x)
        ack_payload = b"\x1e\x05"  # int=30, float=5
        ack_frame = Frame.build(0x16, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        zoom_range = await client.get_zoom_range()

        assert isinstance(zoom_range, ZoomRange)
        assert zoom_range.max_zoom == 30.5

        await client.close()

    @pytest.mark.asyncio
    async def test_get_current_zoom(self, mock_transport: MockTransport) -> None:
        """Test get_current_zoom."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK (uint16 LE zoom x 10: 8.2x -> 82)
        ack_payload = b"\x52\x00"
        ack_frame = Frame.build(0x18, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        zoom = await client.get_current_zoom()

        assert zoom == 8.2

        await client.close()


class TestGimbalCommands:
    """Test gimbal control commands."""

    @pytest.mark.asyncio
    async def test_rotate(self, mock_transport: MockTransport) -> None:
        """Test rotate."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK
        ack_payload = b"\x01"
        ack_frame = Frame.build(0x07, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        await client.rotate(10, -20)

        await client.close()

    @pytest.mark.asyncio
    async def test_one_key_centering(self, mock_transport: MockTransport) -> None:
        """Test one_key_centering."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK
        ack_payload = b"\x01"
        ack_frame = Frame.build(0x08, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        await client.one_key_centering(CenteringAction.CENTER)

        await client.close()

    @pytest.mark.asyncio
    async def test_set_attitude(self, mock_transport: MockTransport) -> None:
        """Test set_attitude."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK (6 bytes: yaw, pitch, roll each int16)
        ack_payload = b"\x00\x00\x00\x00\x00\x00"
        ack_frame = Frame.build(0x0E, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        ack = await client.set_attitude(45.0, -30.0)

        assert isinstance(ack, SetAttitudeAck)

        await client.close()

    @pytest.mark.asyncio
    async def test_set_single_axis(self, mock_transport: MockTransport) -> None:
        """Test set_single_axis."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK (6 bytes)
        ack_payload = b"\x00\x00\x00\x00\x00\x00"
        ack_frame = Frame.build(0x0E, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        ack = await client.set_single_axis("yaw", 90.0)

        assert isinstance(ack, SetAttitudeAck)

        await client.close()

    @pytest.mark.asyncio
    async def test_rotate_nowait(self, mock_transport: MockTransport) -> None:
        """rotate_nowait sends a 0x07 frame with CTRL=0 and does not wait for ACK."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Note: no queue_response — fire-and-forget must not block.
        await client.rotate_nowait(50, -25)

        assert len(mock_transport.sent_frames) == 1
        sent = Frame.from_bytes(mock_transport.sent_frames[0])
        assert sent.cmd_id == 0x07
        assert sent.ctrl == 0  # need_ack must be cleared
        # Payload: 2 x int8 little-endian.
        assert sent.data == b"\x32\xe7"  # 50, -25

        await client.close()

    @pytest.mark.asyncio
    async def test_rotate_nowait_range_validation(self, mock_transport: MockTransport) -> None:
        """rotate_nowait still validates the -100..100 range."""
        from siyi_sdk.exceptions import ConfigurationError

        client = SIYIClient(mock_transport)
        await client.connect()

        with pytest.raises(ConfigurationError):
            await client.rotate_nowait(150, 0)
        with pytest.raises(ConfigurationError):
            await client.rotate_nowait(0, -200)

        # Failed encodes must not have been transmitted.
        assert len(mock_transport.sent_frames) == 0

        await client.close()

    @pytest.mark.asyncio
    async def test_rotate_nowait_high_rate_throughput(self, mock_transport: MockTransport) -> None:
        """Many back-to-back fire-and-forget rotates must not deadlock.

        The standard rotate() serialises on a per-CMD_ID lock waiting for
        ACKs; rotate_nowait() must bypass that and accept a burst.
        """
        client = SIYIClient(mock_transport)
        await client.connect()

        for i in range(200):
            await client.rotate_nowait(i % 100, -(i % 100))

        assert len(mock_transport.sent_frames) == 200
        # Every frame must have CTRL=0.
        for raw in mock_transport.sent_frames:
            assert Frame.from_bytes(raw).ctrl == 0

        await client.close()

    @pytest.mark.asyncio
    async def test_set_attitude_nowait(self, mock_transport: MockTransport) -> None:
        """set_attitude_nowait sends a 0x0E frame with CTRL=0."""
        client = SIYIClient(mock_transport)
        await client.connect()

        await client.set_attitude_nowait(45.0, -30.0)

        assert len(mock_transport.sent_frames) == 1
        sent = Frame.from_bytes(mock_transport.sent_frames[0])
        assert sent.cmd_id == 0x0E
        assert sent.ctrl == 0
        # 4-byte payload, int16 little-endian, angles * 10.
        assert sent.data == b"\xc2\x01\xd4\xfe"  # 450, -300

        await client.close()

    @pytest.mark.asyncio
    async def test_set_single_axis_nowait_yaw(self, mock_transport: MockTransport) -> None:
        """set_single_axis_nowait('yaw', ...) sends 0x41 with axis byte = 0."""
        client = SIYIClient(mock_transport)
        await client.connect()

        await client.set_single_axis_nowait("yaw", 90.0)

        sent = Frame.from_bytes(mock_transport.sent_frames[0])
        assert sent.cmd_id == 0x41
        assert sent.ctrl == 0

        await client.close()

    @pytest.mark.asyncio
    async def test_set_single_axis_nowait_pitch(self, mock_transport: MockTransport) -> None:
        """set_single_axis_nowait('pitch', ...) sends 0x41 with axis byte = 1."""
        client = SIYIClient(mock_transport)
        await client.connect()

        await client.set_single_axis_nowait("pitch", -45.0)

        sent = Frame.from_bytes(mock_transport.sent_frames[0])
        assert sent.cmd_id == 0x41
        assert sent.ctrl == 0

        await client.close()

    @pytest.mark.asyncio
    async def test_get_gimbal_mode(self, mock_transport: MockTransport) -> None:
        """Test get_gimbal_mode."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK (1 byte: mode = LOCK)
        ack_payload = b"\x00"
        ack_frame = Frame.build(0x19, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        mode = await client.get_gimbal_mode()

        assert mode == GimbalMotionMode.LOCK

        await client.close()


class TestAttitudeStreamCommands:
    """Test attitude and stream commands."""

    @pytest.mark.asyncio
    async def test_get_gimbal_attitude(self, mock_transport: MockTransport) -> None:
        """Test get_gimbal_attitude."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK (12 bytes)
        ack_payload = b"\x00\x00" * 6
        ack_frame = Frame.build(CMD_REQUEST_GIMBAL_ATTITUDE, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        attitude = await client.get_gimbal_attitude()

        assert isinstance(attitude, GimbalAttitude)

        await client.close()

    @pytest.mark.asyncio
    async def test_request_gimbal_stream(self, mock_transport: MockTransport) -> None:
        """Test request_gimbal_stream."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK
        ack_payload = b"\x01"
        ack_frame = Frame.build(0x25, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        await client.request_gimbal_stream(GimbalDataType.ATTITUDE, DataStreamFreq.HZ5)

        # Check active streams
        assert GimbalDataType.ATTITUDE in client._gimbal_streams

        await client.close()

    @pytest.mark.asyncio
    async def test_get_magnetic_encoder(self, mock_transport: MockTransport) -> None:
        """Test get_magnetic_encoder."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK (6 bytes)
        ack_payload = b"\x00\x00\x00\x00\x00\x00"
        ack_frame = Frame.build(0x26, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        encoder = await client.get_magnetic_encoder()

        assert isinstance(encoder, MagneticEncoderAngles)

        await client.close()


class TestCameraCommands:
    """Test camera commands."""

    @pytest.mark.asyncio
    async def test_get_camera_system_info(self, mock_transport: MockTransport) -> None:
        """Test get_camera_system_info."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK (8 bytes)
        ack_payload = b"\x00" * 8
        ack_frame = Frame.build(0x0A, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        info = await client.get_camera_system_info()

        assert isinstance(info, CameraSystemInfo)

        await client.close()

    @pytest.mark.asyncio
    async def test_get_encoding_params(self, mock_transport: MockTransport) -> None:
        """Test get_encoding_params."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK (11 bytes)
        ack_payload = struct.pack("<BBHHHB", 1, 1, 1280, 1024, 128, 30)
        ack_frame = Frame.build(0x20, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        params = await client.get_encoding_params(StreamType.MAIN)

        assert isinstance(params, EncodingParams)

        await client.close()

    @pytest.mark.asyncio
    async def test_set_encoding_params(self, mock_transport: MockTransport) -> None:
        """Test set_encoding_params."""
        client = SIYIClient(mock_transport)
        await client.connect()

        params = EncodingParams(
            stream_type=StreamType.MAIN,
            enc_type=VideoEncType.H265,
            resolution_w=1920,
            resolution_h=1080,
            bitrate_kbps=4000,
            frame_rate=30,
        )
        readback = struct.pack("<BBHHHB", 1, 2, 1920, 1080, 4000, 30)
        replies = asyncio.create_task(
            _reply_in_order(
                mock_transport,
                [Frame.build(0x21, b"\x01\x01", seq=0), Frame.build(0x20, readback, seq=0)],
            )
        )

        result = await client.set_encoding_params(params)
        await replies

        assert result is True
        assert [Frame.from_bytes(f).cmd_id for f in mock_transport.sent_frames] == [0x21, 0x20]

        await client.close()

    @pytest.mark.asyncio
    async def test_set_encoding_params_ignored_bitrate(self, mock_transport: MockTransport) -> None:
        """The camera ACKs success even when it keeps the old bitrate."""
        client = SIYIClient(mock_transport)
        await client.connect()

        params = EncodingParams(
            stream_type=StreamType.RECORDING,
            enc_type=VideoEncType.H265,
            resolution_w=3840,
            resolution_h=2160,
            bitrate_kbps=20000,
            frame_rate=30,
        )
        readback = struct.pack("<BBHHHB", 0, 2, 3840, 2160, 15000, 30)
        replies = asyncio.create_task(
            _reply_in_order(
                mock_transport,
                [Frame.build(0x21, b"\x00\x01", seq=0), Frame.build(0x20, readback, seq=0)],
            )
        )

        with pytest.raises(ResponseError):
            await client.set_encoding_params(params)
        await replies

        await client.close()

    @pytest.mark.asyncio
    async def test_format_sd_card(self, mock_transport: MockTransport) -> None:
        """Test format_sd_card."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK
        ack_payload = b"\x01"
        ack_frame = Frame.build(0x48, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        result = await client.format_sd_card()

        assert result is True

        await client.close()

    @pytest.mark.asyncio
    async def test_format_sd_card_waits_past_default_timeout(
        self, mock_transport: MockTransport
    ) -> None:
        """The camera only ACKs 0x48 after the format finishes."""
        client = SIYIClient(mock_transport, default_timeout=0.05)
        await client.connect()

        async def ack_later() -> None:
            await asyncio.sleep(0.2)
            mock_transport.queue_response(Frame.build(0x48, b"\x01", seq=0).to_bytes())

        late_ack = asyncio.create_task(ack_later())
        assert await client.format_sd_card() is True
        await late_ack
        assert len(mock_transport.sent_frames) == 1

        await client.close()

    @pytest.mark.asyncio
    async def test_get_osd_flag(self, mock_transport: MockTransport) -> None:
        """Test get_osd_flag."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK
        ack_payload = b"\x01"  # On
        ack_frame = Frame.build(0x4B, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        flag = await client.get_osd_flag()

        assert flag is True

        await client.close()

    @pytest.mark.asyncio
    async def test_set_osd_flag(self, mock_transport: MockTransport) -> None:
        """Test set_osd_flag."""
        client = SIYIClient(mock_transport)
        await client.connect()

        # Queue ACK
        ack_payload = b"\x01"
        ack_frame = Frame.build(0x4C, ack_payload, seq=0, need_ack=False)
        mock_transport.queue_response(ack_frame.to_bytes())

        result = await client.set_osd_flag(True)

        assert result is True

        await client.close()


class TestUnsupportedOnA8:
    """Commands the A8 mini firmware drops fail fast instead of timing out."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method,args,cmd_id",
        [
            ("get_system_time", (), 0x40),
            ("get_gimbal_system_info", (), 0x31),
            ("get_ip_config", (), 0x81),
            (
                "set_ip_config",
                (
                    IPConfig(
                        ip=IPv4Address("192.168.144.26"),
                        mask=IPv4Address("255.255.255.0"),
                        gateway=IPv4Address("192.168.144.1"),
                    ),
                ),
                0x82,
            ),
            ("request_fc_stream", (FCDataType.ATTITUDE, DataStreamFreq.HZ10), 0x24),
            ("get_picture_name_type", (FileType.PICTURE,), 0x49),
            ("set_picture_name_type", (FileType.PICTURE, FileNameType.INDEX), 0x4A),
            ("get_control_mode", (), 0x27),
            ("get_weak_threshold", (), 0x28),
            (
                "set_weak_threshold",
                (WeakControlThreshold(limit=2.0, voltage=3.0, angular_error=10.0),),
                0x29,
            ),
            ("get_motor_voltage", (), 0x2A),
            ("get_weak_control_mode", (), 0x70),
            ("set_weak_control_mode", (True,), 0x71),
        ],
    )
    async def test_raises_without_sending(
        self, mock_transport: MockTransport, method: str, args: tuple[object, ...], cmd_id: int
    ) -> None:
        client = SIYIClient(mock_transport, default_timeout=5.0)
        await client.connect()

        with pytest.raises(UnsupportedCommandError) as info:
            await asyncio.wait_for(getattr(client, method)(*args), 0.5)
        assert info.value.cmd_id == cmd_id
        assert mock_transport.sent_frames == []

        await client.close()
