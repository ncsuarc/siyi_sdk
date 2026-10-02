# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Video-based point lock for steering the gimbal.

``PointLock`` follows a fixed spot in the scene while the camera moves;
``GimbalPointLock`` uses it to keep the gimbal pointed at that spot. Both need
OpenCV and NumPy (``pip install 'siyi-sdk[tracking]'``) and load on first use.
The rate controller (``LockGains``, ``RateController``, ``pixel_error_deg``)
has no such dependency.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from siyi_sdk.tracking.control import LockGains, RateController, pixel_error_deg

_LAZY = {
    "GimbalPointLock": "siyi_sdk.tracking.gimbal",
    "LockState": "siyi_sdk.tracking.gimbal",
    "LockStatus": "siyi_sdk.tracking.gimbal",
    "PointLock": "siyi_sdk.tracking.point_lock",
    "PointModel": "siyi_sdk.tracking.point_lock",
}


def __getattr__(name: str) -> Any:  # noqa: ANN401 - dynamic lazy re-exports
    """Load OpenCV-backed exports only when requested."""
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        value = getattr(import_module(_LAZY[name]), name)
    except ImportError as exc:
        raise ImportError(
            f"siyi_sdk.tracking.{name} needs OpenCV and NumPy: pip install 'siyi-sdk[tracking]'"
        ) from exc
    globals()[name] = value
    return value


__all__ = [
    "GimbalPointLock",
    "LockGains",
    "LockState",
    "LockStatus",
    "PointLock",
    "PointModel",
    "RateController",
    "pixel_error_deg",
]
