# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""SIYI Gimbal Camera External SDK Protocol — async Python SDK."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from siyi_sdk.client import SIYIClient
from siyi_sdk.convenience import connect_serial, connect_tcp, connect_udp
from siyi_sdk.firmware_tracking import FirmwareTrackingClient
from siyi_sdk.logging_config import configure_logging
from siyi_sdk.media import MediaClient
from siyi_sdk.models import MediaDirectory, MediaFile, MediaType

_STREAM_EXPORTS = frozenset(
    {
        "SIYIStream",
        "StreamBackend",
        "StreamConfig",
        "StreamFrame",
        "StreamState",
        "build_rtsp_url",
    }
)


_TRACKING_EXPORTS = frozenset(
    {
        "AttitudeHistory",
        "FirmwarePointLock",
        "GimbalPointLock",
        "LockGains",
        "LockMetrics",
        "LockState",
        "LockStatus",
        "PointLock",
        "RateController",
    }
)


def __getattr__(name: str) -> Any:  # noqa: ANN401 - dynamic lazy re-exports
    """Load optional video and tracking dependencies only when first requested."""
    if name == "tracking":
        value: Any = import_module("siyi_sdk.tracking")
    elif name in _STREAM_EXPORTS:
        value = getattr(import_module("siyi_sdk.stream"), name)
    elif name in _TRACKING_EXPORTS:
        value = getattr(import_module("siyi_sdk.tracking"), name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


__version__ = "0.6.0"
__all__ = [
    "AttitudeHistory",
    "FirmwarePointLock",
    "FirmwareTrackingClient",
    "GimbalPointLock",
    "LockGains",
    "LockMetrics",
    "LockState",
    "LockStatus",
    "MediaClient",
    "MediaDirectory",
    "MediaFile",
    "MediaType",
    "PointLock",
    "RateController",
    "SIYIClient",
    "SIYIStream",
    "StreamBackend",
    "StreamConfig",
    "StreamFrame",
    "StreamState",
    "build_rtsp_url",
    "configure_logging",
    "connect_serial",
    "connect_tcp",
    "connect_udp",
    "tracking",
]
