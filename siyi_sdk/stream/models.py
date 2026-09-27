# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Data models for SIYI RTSP video streaming.

Defines configuration, frame, and URL-building types used by all streaming backends.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

import numpy as np
from numpy.typing import NDArray

# Maximum exponential back-off delay in seconds before capping.
_RECONNECT_DELAY_CAP: float = 30.0


class CameraGeneration(str, Enum):
    """RTSP URL scheme generation boundary is ZT30.

    OLD refers to cameras released before ZT30 (ZR30, ZR10, A8 Mini, A2 Mini, R1M).
    NEW refers to ZT30, ZT6, and all later models.
    """

    OLD = "old"
    NEW = "new"


CAMERA_GENERATION_MAP: dict[str, CameraGeneration] = {
    "zt30": CameraGeneration.NEW,
    "zt6": CameraGeneration.NEW,
    "zr30": CameraGeneration.OLD,
    "zr10": CameraGeneration.OLD,
    "a8": CameraGeneration.OLD,
    "a2": CameraGeneration.OLD,
    "r1m": CameraGeneration.OLD,
}


class StreamBackend(str, Enum):
    """Video streaming backend selection.

    AUTO probes GStreamer first, then aiortsp, then falls back to OpenCV.
    """

    AUTO = "auto"
    OPENCV = "opencv"
    GSTREAMER = "gstreamer"
    AIORTSP = "aiortsp"


class StreamState(str, Enum):
    """Observable producer lifecycle."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass
class StreamConfig:
    """Configuration for an RTSP video stream.

    Attributes:
        rtsp_url: Full RTSP URL to connect to.
        backend: Backend implementation to use; AUTO probes in order.
        transport: RTSP transport protocol; TCP is preferred for stability.
        latency_ms: GStreamer rtspsrc latency parameter in milliseconds.
        reconnect_delay: Initial reconnection back-off delay in seconds.
        max_reconnect_attempts: Maximum reconnection attempts; 0 means unlimited.
        buffer_size: OpenCV CAP_PROP_BUFFERSIZE value.
    """

    rtsp_url: str
    backend: StreamBackend = StreamBackend.AUTO
    transport: Literal["tcp", "udp"] = "tcp"
    latency_ms: int = 100
    reconnect_delay: float = 2.0
    max_reconnect_attempts: int = 0
    buffer_size: int = 1
    codec: Literal["h264", "h265"] = "h264"
    startup_timeout: float = 5.0
    # GStreamer pipeline override. When set, the GStreamer backend uses this
    # string verbatim instead of its built-in pipelines. Must include an
    # appsink named "sink" producing video/x-raw in BGR or BGRx system memory.
    pipeline: str | None = None

    def __post_init__(self) -> None:
        """Validate configuration fields after initialisation.

        Raises:
            ValueError: If latency_ms < 0, reconnect_delay <= 0, or buffer_size < 1.
        """
        if self.latency_ms < 0:
            raise ValueError(f"latency_ms must be >= 0, got {self.latency_ms}")
        if self.reconnect_delay <= 0:
            raise ValueError(f"reconnect_delay must be > 0, got {self.reconnect_delay}")
        if self.buffer_size < 1:
            raise ValueError(f"buffer_size must be >= 1, got {self.buffer_size}")
        if self.max_reconnect_attempts < 0 or self.startup_timeout <= 0:
            raise ValueError("Reconnect attempts must be nonnegative and startup timeout positive")
        if self.transport not in ("tcp", "udp"):
            raise ValueError("transport must be 'tcp' or 'udp'")
        if self.codec not in ("h264", "h265"):
            raise ValueError(f"codec must be 'h264' or 'h265', got {self.codec!r}")


@dataclass
class StreamFrame:
    """A single decoded video frame from an RTSP stream.

    Attributes:
        frame: BGR image array with shape (H, W, 3).
        timestamp: Monotonic clock timestamp at decode time.
        width: Frame width in pixels.
        height: Frame height in pixels.
        backend: Name of the backend that produced this frame.
    """

    frame: NDArray[np.uint8]
    timestamp: float
    width: int
    height: int
    backend: str


def build_rtsp_url(
    host: str = "192.168.144.25",
    stream: Literal["main", "sub"] = "main",
    generation: CameraGeneration = CameraGeneration.OLD,
) -> str:
    """Return the correct RTSP URL for the given host, stream slot, and camera generation.

    Old-gen cameras (ZR30/ZR10/A8Mini/A2Mini/R1M) expose only a single RTSP stream via
    ``/main.264``; requesting a sub stream raises ValueError.

    New-gen cameras (ZT30/ZT6 and later) expose ``/video1`` (main) and ``/video2`` (sub).

    Args:
        host: Camera IP address.
        stream: "main" for primary stream, "sub" for secondary (new-gen only).
        generation: Camera generation determining the URL path scheme.

    Returns:
        Full RTSP URL string.

    Example:
        >>> build_rtsp_url(generation=CameraGeneration.NEW, stream="sub")
        'rtsp://192.168.144.25:8554/video2'
        >>> build_rtsp_url(generation=CameraGeneration.OLD)
        'rtsp://192.168.144.25:8554/main.264'
    """
    generation = CameraGeneration(generation)
    if stream not in ("main", "sub"):
        raise ValueError("stream must be 'main' or 'sub'")
    if generation is CameraGeneration.OLD:
        if stream != "main":
            raise ValueError("Old-generation cameras, including A8 Mini, have no sub stream")
        return f"rtsp://{host}:8554/main.264"
    path = "video1" if stream == "main" else "video2"
    return f"rtsp://{host}:8554/{path}"
