"""A8 Mini RTSP URL and stream model checks."""

from __future__ import annotations

import numpy as np
import pytest

from siyi_sdk.stream.models import StreamBackend, StreamConfig, StreamFrame, build_rtsp_url


class TestBuildRtspUrl:
    def test_default_host(self) -> None:
        assert build_rtsp_url() == "rtsp://192.168.144.25:8554/main.264"

    def test_custom_host(self) -> None:
        assert build_rtsp_url("10.0.0.2") == "rtsp://10.0.0.2:8554/main.264"


class TestStreamConfig:
    def test_defaults(self) -> None:
        cfg = StreamConfig(rtsp_url="rtsp://example.com/main.264")
        assert cfg.backend is StreamBackend.AUTO
        assert cfg.transport == "tcp"
        assert cfg.latency_ms == 100
        assert cfg.reconnect_delay == 2.0
        assert cfg.max_reconnect_attempts == 0
        assert cfg.buffer_size == 1

    def test_invalid_latency_ms(self) -> None:
        with pytest.raises(ValueError, match="latency_ms"):
            StreamConfig(rtsp_url="rtsp://x", latency_ms=-1)

    def test_invalid_reconnect_delay_zero(self) -> None:
        with pytest.raises(ValueError, match="reconnect_delay"):
            StreamConfig(rtsp_url="rtsp://x", reconnect_delay=0)

    def test_invalid_reconnect_delay_negative(self) -> None:
        with pytest.raises(ValueError, match="reconnect_delay"):
            StreamConfig(rtsp_url="rtsp://x", reconnect_delay=-1.0)

    def test_invalid_buffer_size_zero(self) -> None:
        with pytest.raises(ValueError, match="buffer_size"):
            StreamConfig(rtsp_url="rtsp://x", buffer_size=0)

    def test_latency_ms_zero_is_valid(self) -> None:
        cfg = StreamConfig(rtsp_url="rtsp://x", latency_ms=0)
        assert cfg.latency_ms == 0

    def test_custom_values(self) -> None:
        cfg = StreamConfig(
            rtsp_url="rtsp://x",
            backend=StreamBackend.OPENCV,
            transport="udp",
            latency_ms=50,
            reconnect_delay=5.0,
            max_reconnect_attempts=3,
            buffer_size=2,
        )
        assert cfg.backend is StreamBackend.OPENCV
        assert cfg.transport == "udp"
        assert cfg.latency_ms == 50
        assert cfg.reconnect_delay == 5.0
        assert cfg.max_reconnect_attempts == 3
        assert cfg.buffer_size == 2


class TestStreamFrame:
    def test_stores_array(self) -> None:
        arr = np.zeros((480, 640, 3), dtype=np.uint8)
        sf = StreamFrame(frame=arr, timestamp=1.0, width=640, height=480, backend="test")
        assert sf.frame is arr
        assert sf.width == 640
        assert sf.height == 480
        assert sf.timestamp == 1.0
        assert sf.backend == "test"

    def test_frame_is_mutable(self) -> None:
        arr = np.zeros((100, 100, 3), dtype=np.uint8)
        sf = StreamFrame(frame=arr, timestamp=0.0, width=100, height=100, backend="test")
        sf.frame[0, 0, 0] = 255
        assert arr[0, 0, 0] == 255
