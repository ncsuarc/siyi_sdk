# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Turn a pointing error into gimbal rotation-speed commands.

Pure Python with no OpenCV dependency, so it can be unit tested and reused with
any tracker that reports a pixel position.

The controller works in physical units (degrees, degrees per second) and
converts to the -100..100 speed units of command 0x07 through a measured
:class:`LoopModel`. Gains then follow from the measured delays instead of
trial and error; see :func:`LockGains.for_model`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class LoopModel:
    """Measured behaviour of one gimbal and video link (see ``siyi_sdk.tracking.calibrate``).

    Attributes:
        deg_per_unit: (yaw, pitch) turn rate in deg/s for one 0x07 speed unit.
        command_delay_s: Time from sending 0x07 until the attitude stream shows motion.
        video_delay_s: Time from the camera seeing something until the decoded frame
            arrives (frame ``timestamp`` minus capture time).
    """

    deg_per_unit: tuple[float, float] = (1.0, 1.0)
    command_delay_s: float = 0.06
    video_delay_s: float = 0.2


@dataclass
class LockGains:
    """PI gains in physical units plus output limits.

    ``kp`` is the loop bandwidth in 1/s: a 1 degree error commands ``kp`` deg/s.
    ``ki`` (1/s^2) removes steady error left by imperfect feedforward. The pure
    time delay in the loop limits ``kp``: treating the measured delay (dead
    time plus motor lag) as pure delay, ``kp = 1 / (2 * delay)`` leaves about
    60 degrees of phase margin, which :meth:`for_model` uses.
    """

    kp: float = 3.0
    ki: float = 1.0
    max_speed: int = 100  # 0x07 units
    deadband_deg: float = 0.15  # errors smaller than this command no correction
    max_rate_deg_s: float = 120.0
    # Accumulate the integral only near the target, so a large initial error
    # can't wind it up and cause an overshoot.
    integral_zone_deg: float = 2.0

    @classmethod
    def for_model(cls, model: LoopModel, *, compensated: bool = True) -> LockGains:
        """Gains for a measured loop.

        With attitude-based delay compensation the only delay left inside the
        loop is the command delay; without it the video delay is in the loop too.
        """
        delay = model.command_delay_s + (0.0 if compensated else model.video_delay_s)
        kp = 1.0 / (2.0 * max(delay, 0.02))
        return cls(kp=kp, ki=kp * kp / 10.0)


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
    """Per-axis PI plus feedforward, output in 0x07 speed units.

    ``update`` takes the current pointing error (degrees) and the target's own
    angular velocity (deg/s, the feedforward). Feedforward lets the gimbal
    follow a target that moves steadily across the sky, as a fixed ground spot
    does when the aircraft flies past, without needing a large error first.
    """

    def __init__(self, gains: LockGains | None = None, model: LoopModel | None = None) -> None:
        """Create a controller; ``model`` converts deg/s to speed units."""
        self.gains = gains or LockGains()
        self.model = model or LoopModel()
        self.reset()

    def reset(self) -> None:
        """Clear the integral terms (call when tracking starts or stops)."""
        self._integral = [0.0, 0.0]

    def update(
        self,
        error_yaw: float,
        error_pitch: float,
        dt: float,
        feedforward: tuple[float, float] = (0.0, 0.0),
    ) -> tuple[int, int]:
        """Return (yaw, pitch) speeds in 0x07 units for errors in degrees."""
        g = self.gains
        dt = min(max(dt, 0.0), 0.2)  # a stall must not wind up the integral
        out = []
        for axis, error in enumerate((error_yaw, error_pitch)):
            if abs(error) < g.deadband_deg:
                error = 0.0
            elif abs(error) < g.integral_zone_deg:
                self._integral[axis] += error * dt
            if g.ki:
                # Anti-windup: the integral alone may never ask for more than full rate.
                limit = g.max_rate_deg_s / g.ki
                self._integral[axis] = max(-limit, min(limit, self._integral[axis]))
            rate = g.kp * error + g.ki * self._integral[axis] + feedforward[axis]
            rate = max(-g.max_rate_deg_s, min(g.max_rate_deg_s, rate))
            per_unit = self.model.deg_per_unit[axis]
            units = rate / per_unit if abs(per_unit) > 1e-3 else 0.0
            out.append(round(max(-g.max_speed, min(g.max_speed, units))))
        return out[0], out[1]
