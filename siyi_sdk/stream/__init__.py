"""SIYI camera RTSP video streaming.

Provides SIYIStream and related types for receiving live video from SIYI cameras
via RTSP. Three backends are supported: OpenCV (default fallback), GStreamer
(lowest latency, hardware acceleration), and aiortsp + PyAV (pure-Python async).
"""

from __future__ import annotations

from .models import (
    StreamBackend,
    StreamConfig,
    StreamFrame,
    StreamState,
    build_rtsp_url,
)
from .stream import SIYIStream

__all__ = [
    "SIYIStream",
    "StreamBackend",
    "StreamConfig",
    "StreamFrame",
    "StreamState",
    "build_rtsp_url",
]
