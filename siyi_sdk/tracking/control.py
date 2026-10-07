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
from collections import deque
from dataclasses import dataclass


@dataclass
class LoopModel:
    """Measured behaviour of one gimbal and video link (see ``siyi_sdk.tracking.calibrate``).

    Attributes:
        deg_per_unit: (yaw, pitch) turn rate in deg/s for one 0x07 speed unit.
        command_delay_s: Time from sending 0x07 until the attitude stream shows motion.
        video_delay_s: Time from the camera seeing something until the decoded frame
            arrives (frame ``timestamp`` minus capture time).
        motor_tau_s: The part of ``command_delay_s`` that is the motor getting up to
            speed (a first-order lag) rather than dead time; 0 when not measured.
    """

    deg_per_unit: tuple[float, float] = (1.0, 1.0)
    command_delay_s: float = 0.06
    video_delay_s: float = 0.2
    motor_tau_s: float = 0.0

    @property
    def dead_time_s(self) -> float:
        """Time from sending 0x07 until the motor starts to respond at all."""
        return max(0.0, self.command_delay_s - self.motor_tau_s)


# With the dead time predicted away, the loop is still not ideal: attitude reports lag a
# little and the model is approximate. Treat this much extra delay as unmodelled.
SMITH_MARGIN_S = 0.02
MAX_KP = 15.0  # 1/s; beyond this tracker noise drives the motor more than the target does


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
    # Hysteresis deadband: once the error is under ``deadband_deg`` with the target
    # nearly still, corrections stop until the error exceeds ``release_deg``. A single
    # threshold made the motor flicker on and off around it.
    deadband_deg: float = 0.10
    release_deg: float = 0.25
    still_rate_deg_s: float = 1.0  # "nearly still" for the deadband
    max_rate_deg_s: float = 120.0
    # Limit on how fast the commanded rate may change (deg/s^2); None for no limit.
    max_accel_deg_s2: float | None = None
    # Accumulate the integral only near the target, so a large initial error
    # can't wind it up and cause an overshoot.
    integral_zone_deg: float = 2.0

    @classmethod
    def for_model(
        cls, model: LoopModel, *, compensated: bool = True, smith: bool = False
    ) -> LockGains:
        """Gains for a measured loop.

        With attitude-based delay compensation the only delay left inside the
        loop is the command delay; without it the video delay is in the loop too.
        With a :class:`GimbalPredictor` (``smith``) the dead time leaves the loop as
        well, and only the motor lag plus a margin for model error limits the gain.
        """
        if smith and model.motor_tau_s > 0:
            delay = model.motor_tau_s + SMITH_MARGIN_S
        else:
            delay = model.command_delay_s + (0.0 if compensated else model.video_delay_s)
        kp = min(1.0 / (2.0 * max(delay, 0.02)), MAX_KP)
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


class GimbalPredictor:
    """Where the gimbal will be once the speed commands already sent take effect.

    The core of a Smith predictor. A 0x07 command moves the gimbal only after the
    dead time, and then through the motor's lag; a controller comparing the target
    with the *current* attitude keeps adding correction for error that commands in
    flight are already removing, and overshoots. That forces a low gain. Comparing
    with the gimbal's position one dead time ahead (the current attitude plus what
    the commands in flight will still turn it) takes the dead time out of the loop,
    so the gain can follow the much shorter motor lag.

    The prediction starts from the measured attitude every step, so model errors
    don't accumulate; they only bias the extrapolation over one dead time.
    """

    STEP_S = 0.005

    def __init__(self, dead_time_s: float, motor_tau_s: float) -> None:
        """Model the gimbal as dead time plus a first-order motor lag."""
        self.dead_time_s = dead_time_s
        self.motor_tau_s = motor_tau_s
        # (send time, commanded yaw rate, pitch rate) in image-aligned deg/s.
        self._sent: deque[tuple[float, float, float]] = deque()

    def reset(self) -> None:
        self._sent.clear()

    def record(self, t: float, rate: tuple[float, float]) -> None:
        """Note that the rate ``rate`` (deg/s) was commanded at time ``t``."""
        if self._sent and self._sent[-1][1:] == rate:
            return  # unchanged; the step function already holds it
        self._sent.append((t, rate[0], rate[1]))
        # Keep what can still matter: the newest command older than the history window.
        horizon = t - 2 * self.dead_time_s - 5 * self.motor_tau_s - 0.1
        while len(self._sent) > 1 and self._sent[1][0] < horizon:
            self._sent.popleft()

    def _commanded(self, t: float) -> tuple[float, float]:
        """The rate the motor is being driven toward at ``t`` (sent one dead time earlier)."""
        rate = (0.0, 0.0)
        for sent, yaw, pitch in self._sent:
            if sent > t - self.dead_time_s:
                break
            rate = (yaw, pitch)
        return rate

    def turn_ahead(self, now: float) -> tuple[float, float]:
        """Degrees the gimbal will still turn during the next dead time (yaw, pitch)."""
        if not self._sent or self.dead_time_s <= 0:
            return (0.0, 0.0)
        tau, step = self.motor_tau_s, self.STEP_S
        # Run the motor model from well before now (so its speed now is settled) to one
        # dead time ahead, integrating the turn only over the future part.
        t = now - 5 * tau - step
        speed = list(self._commanded(t))
        turned = [0.0, 0.0]
        end = now + self.dead_time_s
        while t < end:
            target = self._commanded(t)
            blend = 1.0 if tau <= 0 else min(1.0, step / tau)
            for axis in (0, 1):
                speed[axis] += (target[axis] - speed[axis]) * blend
                if t >= now:
                    turned[axis] += speed[axis] * step
            t += step
        return turned[0], turned[1]


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
        """Clear the integral and output state (call when tracking starts or stops)."""
        self._integral = [0.0, 0.0]
        self._holding = [False, False]
        self._rate = [0.0, 0.0]  # last commanded rate, deg/s
        self._remainder = [0.0, 0.0]  # rounding left over from previous commands, units

    def update(
        self,
        error_yaw: float,
        error_pitch: float,
        dt: float,
        feedforward: tuple[float, float] = (0.0, 0.0),
        *,
        integrate: bool = True,
    ) -> tuple[int, int]:
        """Return (yaw, pitch) speeds in 0x07 units for errors in degrees.

        ``integrate=False`` (after a stalled loop) steers on the error without letting
        the gap accumulate into the integral.
        """
        g = self.gains
        dt = min(max(dt, 0.0), 0.2)  # a stall must not wind up the integral
        out = []
        for axis, error in enumerate((error_yaw, error_pitch)):
            still = abs(feedforward[axis]) < g.still_rate_deg_s
            if self._holding[axis]:
                self._holding[axis] = still and abs(error) <= g.release_deg
            else:
                self._holding[axis] = still and abs(error) < g.deadband_deg
            if self._holding[axis]:
                # On target with the target still: send nothing. Lock mode holds the
                # gimbal by itself, while the integral and a noise-level feedforward
                # would make it creep out of the deadband and kick back, over and over.
                self._rate[axis] = 0.0
                self._remainder[axis] = 0.0
                out.append(0)
                continue
            if integrate and abs(error) < g.integral_zone_deg:
                self._integral[axis] += error * dt
            if g.ki:
                # Anti-windup: the integral alone may never ask for more than full rate.
                limit = g.max_rate_deg_s / g.ki
                self._integral[axis] = max(-limit, min(limit, self._integral[axis]))
            rate = g.kp * error + g.ki * self._integral[axis] + feedforward[axis]
            rate = max(-g.max_rate_deg_s, min(g.max_rate_deg_s, rate))
            if g.max_accel_deg_s2 is not None and dt > 0:
                step = g.max_accel_deg_s2 * dt
                rate = max(self._rate[axis] - step, min(self._rate[axis] + step, rate))
            self._rate[axis] = rate
            out.append(self._to_units(axis, rate))
        return out[0], out[1]

    def _to_units(self, axis: int, rate: float) -> int:
        """Convert deg/s to whole 0x07 units, carrying the rounding into the next step.

        One unit is about 0.7 deg/s on the A8 Mini, so plain rounding turns a wanted
        0.3 deg/s into nothing and 1.2 deg/s into 0.7 forever. Carrying the remainder
        makes the average speed over a few steps exact, which stops the hunting near
        the target.
        """
        g = self.gains
        per_unit = self.model.deg_per_unit[axis]
        if abs(per_unit) <= 1e-3 or rate == 0.0:
            self._remainder[axis] = 0.0
            return 0
        exact = rate / per_unit + self._remainder[axis]
        units = max(-g.max_speed, min(g.max_speed, round(exact)))
        # Only carry what rounding lost; a saturated command keeps no debt.
        self._remainder[axis] = exact - units if abs(units) < g.max_speed else 0.0
        self._remainder[axis] = max(-1.0, min(1.0, self._remainder[axis]))
        return units
