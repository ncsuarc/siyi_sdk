# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Turn an image-space pointing error into gimbal rotation-speed commands.

Pure Python with no OpenCV dependency, so it can be unit tested and reused with
any tracker that reports a pixel position.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class LockGains:
    """Proportional-integral gains and limits for the rate loop.

    Speeds are the -100..100 units of command 0x07 (rotate). The camera does
    not document how many degrees per second one unit is, so tune ``kp`` on the
    real gimbal: raise it until the camera starts to overshoot, then halve it.
    Video delay (about 0.2 s) limits how fast this loop can be.
    """

    kp: float = 2.0  # speed units per degree of error
    ki: float = 0.5  # speed units per degree-second of accumulated error
    max_speed: int = 60
    deadband_deg: float = 0.3  # errors smaller than this command no motion
    max_step: int = 20  # largest change in speed between updates (smooths starts/stops)


def pixel_error_deg(
    x: float, y: float, width: int, height: int, *, hfov_deg: float, zoom: float = 1.0
) -> tuple[float, float]:
    """Angle from the image centre to pixel (x, y): (yaw right +, pitch up +) in degrees.

    Uses a pinhole lens model; digital zoom narrows the field of view as
    ``tan(hfov / 2) / zoom``. Works for points outside the image too.
    """
    tan_half = math.tan(math.radians(hfov_deg) / 2) / zoom
    yaw = math.degrees(math.atan((2 * x / width - 1) * tan_half))
    pitch = -math.degrees(math.atan((2 * y / height - 1) * tan_half * height / width))
    return yaw, pitch


class RateController:
    """Two independent PI loops (yaw, pitch) with deadband, saturation and slew limits."""

    def __init__(self, gains: LockGains | None = None) -> None:
        """Create a controller with ``gains`` (defaults to :class:`LockGains`)."""
        self.gains = gains or LockGains()
        self.reset()

    def reset(self) -> None:
        """Clear the integral terms and the last command (call when tracking stops)."""
        self._integral = [0.0, 0.0]
        self._last = [0, 0]

    def update(self, error_yaw: float, error_pitch: float, dt: float) -> tuple[int, int]:
        """Return (yaw, pitch) speeds for the current error in degrees."""
        g = self.gains
        dt = min(max(dt, 0.0), 0.5)  # ignore stalls so one gap can't wind up the integral
        out = []
        for axis, error in enumerate((error_yaw, error_pitch)):
            if abs(error) < g.deadband_deg:
                error = 0.0
                self._integral[axis] *= 0.9  # bleed off slowly instead of jumping
            else:
                self._integral[axis] += error * dt
            # Anti-windup: the integral alone may never exceed full speed.
            limit = g.max_speed / g.ki if g.ki else 0.0
            self._integral[axis] = max(-limit, min(limit, self._integral[axis]))
            wanted = g.kp * error + g.ki * self._integral[axis]
            wanted = max(-g.max_speed, min(g.max_speed, wanted))
            step = max(-g.max_step, min(g.max_step, wanted - self._last[axis]))
            self._last[axis] = round(self._last[axis] + step)
            out.append(self._last[axis])
        return out[0], out[1]
