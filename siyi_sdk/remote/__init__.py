# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Remote control of an A8 Mini through a camera service on the aircraft.

The service (:class:`CameraServer` around a :class:`CameraSession`) runs on the
computer wired to the camera and does everything latency-sensitive there:
point lock, the speed-setpoint deadman, telemetry. A ground station uses
:class:`RemoteCamera`, which mirrors :class:`siyi_sdk.SIYIClient`, over one
websocket carrying JSON-RPC 2.0. See ``docs/remote.md``.

The ground side needs ``pip install 'siyi-sdk[remote]'``; the service needs
``'siyi-sdk[remote-server]'``. Imports are lazy so the client does not pull in
the service's dependencies.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from siyi_sdk.remote.protocol import (
    ConflictError,
    NotControllerError,
    OperationFailedError,
    RemoteError,
    ServiceUnavailableError,
    UnknownMethodError,
)

_LAZY = {
    "RemoteCamera": "siyi_sdk.remote.client",
    "CameraServer": "siyi_sdk.remote.server",
    "CameraSession": "siyi_sdk.remote.session",
    "LockConfig": "siyi_sdk.remote.lock_config",
}

__all__ = [
    "CameraServer",
    "CameraSession",
    "ConflictError",
    "LockConfig",
    "NotControllerError",
    "OperationFailedError",
    "RemoteCamera",
    "RemoteError",
    "ServiceUnavailableError",
    "UnknownMethodError",
]


def __getattr__(name: str) -> Any:  # noqa: ANN401 - dynamic lazy re-exports
    """Load the client or service only when first requested."""
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(_LAZY[name]), name)
    globals()[name] = value
    return value
