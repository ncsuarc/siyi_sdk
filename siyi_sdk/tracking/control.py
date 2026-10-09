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
        deadzone_units: (yaw, pitch) offset of the speed line: above the threshold the
            rate is ``deg_per_unit * (units - deadzone)``. 0 when not measured.
        min_units: (yaw, pitch) slowest 0x07 speed the motor obeys at all; smaller
            commands don't move it. 0 when not measured.
    """

    deg_per_unit: tuple[float, float] = (1.0, 1.0)
    command_delay_s: float = 0.06
    video_delay_s: float = 0.2
    motor_tau_s: float = 0.0
    deadzone_units: tuple[float, float] = (0.0, 0.0)
    min_units: tuple[float, float] = (0.0, 0.0)

    def delivered(self, axis: int, units: float) -> float:
        """Turn rate (deg/s) a 0x07 speed actually produces on ``axis``."""
        magnitude = abs(units)
        if magnitude == 0 or magnitude < self.min_units[axis]:
            return 0.0
        effective = max(0.0, magnitude - self.deadzone_units[axis])
        return math.copysign(effective * self.deg_per_unit[axis], units)

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
    # Sized to the A8 Mini: its slowest obeyed speed (about 6 units, 5 deg/s) for one
    # 50 Hz step moves it about 0.1 deg, so a tighter band can't be settled into and the
    # lock pulses back and forth forever; the target-velocity estimate of a still spot
    # wanders by 1-2 deg/s.
    deadband_deg: float = 0.2
    release_deg: float = 0.4
    still_rate_deg_s: float = 2.5  # "nearly still" for the deadband
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

    def __init__(self, dead_time_s: float, motor_tau_s: float, history_s: float = 0.1) -> None:
        """Model the gimbal as dead time plus a first-order motor lag.

        ``history_s`` is how far into the past :meth:`turned` must still reach.
        """
        self.dead_time_s = dead_time_s
        self.motor_tau_s = motor_tau_s
        self.history_s = history_s
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
        horizon = t - 2 * self.dead_time_s - 5 * self.motor_tau_s - self.history_s
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
        if self.dead_time_s <= 0:
            return (0.0, 0.0)
        return self.turned(now, now + self.dead_time_s)

    def turned(self, start: float, end: float) -> tuple[float, float]:
        """Degrees the recorded commands turn the gimbal between ``start`` and ``end``."""
        if not self._sent:
            return (0.0, 0.0)
        tau, step = self.motor_tau_s, self.STEP_S
        # Run the motor model from well before start (so its speed then is settled),
        # integrating the turn only from start on.
        t = start - 5 * tau - step
        speed = list(self._commanded(t))
        turned = [0.0, 0.0]
        while t < end:
            target = self._commanded(t)
            blend = 1.0 if tau <= 0 else min(1.0, step / tau)
            for axis in (0, 1):
                speed[axis] += (target[axis] - speed[axis]) * blend
                if t >= start:
                    turned[axis] += speed[axis] * step
            t += step
        return turned[0], turned[1]


class TurnRateEstimator:
    """Learn, while locked, how far the gimbal really turns per 0x07 speed unit.

    Calibration measures ``deg_per_unit`` once, but the real figure drifts: battery
    voltage sags, payloads and mounts differ, and one calibration is a few seconds of
    data. A wrong figure changes the loop gain (too high oscillates, too low lags) and
    biases the Smith predictor.

    Over a sliding window, this compares the turn the attitude stream shows with the
    turn the :class:`LoopModel` predicts for the commands sent (dead time and motor
    lag included), and keeps a per-axis ``scale``: actual turn / predicted turn. It is
    a one-parameter recursive least-squares fit with forgetting::

        scale = sum(predicted * actual) / sum(predicted ** 2)

    Safeguards, since a bad estimate is worse than none:

    - Only windows where the gimbal was told to turn at least ``min_turn_deg`` count;
      a still target says nothing about the turn rate.
    - Windows where the actual turn disagrees with the prediction by more than
      ``outlier_ratio`` are dropped: the mount turned by itself (aircraft yaw, a hand on
      the rig) or the gimbal hit a stop.
    - Windows holding a saturated command are dropped (see :meth:`saturated`): the
      motor's top speed is not the speed line extended.
    - ``scale`` is clamped to ``limits`` and starts at 1 with the weight of
      ``prior_deg`` degrees of turn, so it moves only on consistent evidence.

    It refines the dead time the same way (``dead_time_s``): every ``refit_every``
    accepted windows it tries dead times around the current one against the last
    ``refit_s`` seconds and moves toward the best fit, but only when the fit clearly
    prefers one (with too little motion every dead time fits alike). The dead time
    stays within ``dead_time_limits`` times the calibrated one.
    """

    def __init__(
        self,
        dead_time_s: float,
        motor_tau_s: float,
        *,
        window_s: float = 0.2,
        min_turn_deg: float = 0.5,
        forget: float = 0.99,
        prior_deg: float = 2.0,
        limits: tuple[float, float] = (0.5, 2.0),
        outlier_ratio: float = 3.0,
        refit_every: int = 25,
        refit_s: float = 2.0,
        dead_time_limits: tuple[float, float] = (0.5, 1.5),
    ) -> None:
        """Create an estimator for a gimbal with the given dead time and motor lag."""
        self.calibrated_dead_time_s = dead_time_s
        self.refit_every = refit_every
        self.refit_s = refit_s
        self.dead_time_limits = dead_time_limits
        # Commands (t, yaw, pitch) and accepted windows (t, turn yaw, turn pitch) for the
        # dead-time fit; a turn is None on an axis whose window wasn't accepted.
        self._log: deque[tuple[float, float, float]] = deque()
        self._windows: deque[tuple[float, float | None, float | None]] = deque()
        self._since_refit = 0
        self.window_s = window_s
        self.min_turn_deg = min_turn_deg
        self.forget = forget
        self.limits = limits
        self.outlier_ratio = outlier_ratio
        self.model = GimbalPredictor(dead_time_s, motor_tau_s, history_s=window_s + 0.1)
        self.scale = [1.0, 1.0]
        self.samples = [0, 0]  # windows accepted per axis
        self._num = [prior_deg**2, prior_deg**2]  # sum(predicted * actual)
        self._den = [prior_deg**2, prior_deg**2]  # sum(predicted ** 2)
        self._blocked_until = [-math.inf, -math.inf]

    @property
    def dead_time_s(self) -> float:
        """Current dead-time estimate: from sending 0x07 until the motor responds."""
        return self.model.dead_time_s

    def reset_history(self) -> None:
        """Forget the commands sent (a new lock); the learned scale and dead time are kept."""
        self.model.reset()
        self._log.clear()
        self._windows.clear()
        self._blocked_until = [-math.inf, -math.inf]

    def record(self, t: float, rate: tuple[float, float]) -> None:
        """Note the rate (deg/s) the *calibrated* model expects from the command sent at ``t``."""
        self.model.record(t, rate)
        if not self._log or self._log[-1][1:] != rate:
            self._log.append((t, rate[0], rate[1]))
        horizon = t - self.refit_s - self.window_s - 2 * self.dead_time_s - 0.2
        while len(self._log) > 1 and self._log[1][0] < horizon:
            self._log.popleft()

    def saturated(self, t: float, axis: int) -> None:
        """Note that ``axis`` was commanded at full speed at ``t``; skip windows holding it."""
        model = self.model
        settle = model.dead_time_s + 5 * model.motor_tau_s + self.window_s
        self._blocked_until[axis] = t + settle

    def observe(self, t: float, actual: tuple[float, float], previous: tuple[float, float]) -> None:
        """Fit one window: attitude ``actual`` at ``t`` and ``previous`` at ``t - window_s``.

        Both are image-aligned degrees.
        """
        predicted = self.model.turned(t - self.window_s, t)
        accepted: list[float | None] = [None, None]
        for axis in (0, 1):
            p = predicted[axis]
            a = actual[axis] - previous[axis]
            if t < self._blocked_until[axis] or abs(p) < self.min_turn_deg:
                continue
            if not 1 / self.outlier_ratio <= a / p <= self.outlier_ratio:
                continue
            self._num[axis] = self.forget * self._num[axis] + p * a
            self._den[axis] = self.forget * self._den[axis] + p * p
            low, high = self.limits
            self.scale[axis] = min(high, max(low, self._num[axis] / self._den[axis]))
            self.samples[axis] += 1
            accepted[axis] = a
        if accepted == [None, None]:
            return
        self._windows.append((t, accepted[0], accepted[1]))
        while self._windows and self._windows[0][0] < t - self.refit_s:
            self._windows.popleft()
        self._since_refit += 1
        if self._since_refit >= self.refit_every:
            self._since_refit = 0
            self._refine_dead_time()

    def _turns(self, dead_time: float) -> list[tuple[float, float]]:
        """Predicted (yaw, pitch) turn over each stored window, for a given dead time."""
        windows, log = self._windows, self._log
        tau, step = self.model.motor_tau_s, GimbalPredictor.STEP_S
        start = windows[0][0] - self.window_s - 5 * tau - step
        count = int((windows[-1][0] - start) / step) + 2
        blend = 1.0 if tau <= 0 else min(1.0, step / tau)
        # Turned angle on a uniform time grid, by the same motor model as turned().
        angle = [(0.0, 0.0)] * count
        speed = [0.0, 0.0]
        turned = [0.0, 0.0]
        index = -1
        for i in range(count):
            sent = start + i * step - dead_time
            while index + 1 < len(log) and log[index + 1][0] <= sent:
                index += 1
            target = log[index][1:] if index >= 0 else (0.0, 0.0)
            for axis in (0, 1):
                speed[axis] += (target[axis] - speed[axis]) * blend
                turned[axis] += speed[axis] * step
            angle[i] = (turned[0], turned[1])

        def at(t: float, axis: int) -> float:
            x = min(max((t - start) / step, 0.0), count - 1.0)
            low = min(int(x), count - 2)
            return angle[low][axis] + (angle[low + 1][axis] - angle[low][axis]) * (x - low)

        return [
            (
                at(t, 0) - at(t - self.window_s, 0),
                at(t, 1) - at(t - self.window_s, 1),
            )
            for t, _, _ in windows
        ]

    def _refine_dead_time(self) -> None:
        """Move ``dead_time_s`` toward the dead time that best explains the stored windows."""
        if len(self._windows) < 15 or not self._log:
            return
        current = self.dead_time_s
        low = self.calibrated_dead_time_s * self.dead_time_limits[0]
        high = self.calibrated_dead_time_s * self.dead_time_limits[1] + 0.02
        candidates = sorted({min(high, max(low, current + 0.01 * k)) for k in range(-4, 5)})
        costs = []
        for dead_time in candidates:
            cost = 0.0
            for (_, *actual), predicted in zip(self._windows, self._turns(dead_time)):
                for axis in (0, 1):
                    if actual[axis] is not None:
                        cost += (actual[axis] - self.scale[axis] * predicted[axis]) ** 2
            costs.append(cost)
        best = min(range(len(costs)), key=costs.__getitem__)
        # Without enough motion every dead time fits alike: keep the estimate.
        if max(costs) < 1.5 * costs[best] + 1e-3:
            return
        self.model.dead_time_s = 0.7 * current + 0.3 * candidates[best]


class OscillationGuard:
    """Back off the loop gain while the pointing error keeps swinging back and forth.

    The gains follow from the measured delays; when the measurement is off, the mount
    wobbles, or the delays grow (a busy link), the loop can ring instead of settling.
    A ringing loop shows as the error flipping sign again and again with real
    amplitude: ``half_cycles`` swings past +-``amplitude_deg`` within ``window_s``. Each
    time that is seen, ``scale`` drops by ``backoff`` (never below ``floor``); after
    ``calm_s`` without ringing it creeps back toward 1 at ``recover_per_s``.

    Swings smaller than ``amplitude_deg`` (deadband pulsing, tracker noise) and a
    single overshoot don't count.
    """

    def __init__(
        self,
        *,
        amplitude_deg: float = 0.5,
        half_cycles: int = 4,
        window_s: float = 3.0,
        backoff: float = 0.7,
        floor: float = 0.3,
        calm_s: float = 4.0,
        recover_per_s: float = 0.05,
    ) -> None:
        """Create a guard; ``scale`` starts at 1."""
        self.amplitude_deg = amplitude_deg
        self.half_cycles = half_cycles
        self.window_s = window_s
        self.backoff = backoff
        self.floor = floor
        self.calm_s = calm_s
        self.recover_per_s = recover_per_s
        self.enabled = True
        self.scale = 1.0
        self.backoffs = 0  # times the gain was cut
        self._t = 0.0
        self._last_backoff = -math.inf
        self.reset()

    def reset(self) -> None:
        """Forget the swing history (a new lock); the current scale is kept."""
        self._side = [0, 0]
        self._flips: list[deque[float]] = [deque(), deque()]

    def update(self, errors: tuple[float, float], dt: float) -> float:
        """Account for one control step; returns the gain scale to use."""
        if not self.enabled:
            return 1.0
        self._t += dt
        t = self._t
        for axis, error in enumerate(errors):
            side = 1 if error > self.amplitude_deg else -1 if error < -self.amplitude_deg else 0
            if side == 0:
                continue
            flips = self._flips[axis]
            if self._side[axis] == -side:
                flips.append(t)
            self._side[axis] = side
            while flips and flips[0] < t - self.window_s:
                flips.popleft()
            if len(flips) >= self.half_cycles:
                self.scale = max(self.floor, self.scale * self.backoff)
                self.backoffs += 1
                self._last_backoff = t
                for f in self._flips:
                    f.clear()
        if t - self._last_backoff > self.calm_s:
            self.scale = min(1.0, self.scale + self.recover_per_s * dt)
        return self.scale


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
        # Correction to ``model.deg_per_unit`` learned while running (TurnRateEstimator):
        # the gimbal turns ``deg_per_unit * turn_scale`` deg/s per unit. Kept across resets.
        self.turn_scale = [1.0, 1.0]
        # Cuts kp (and ki by its square, keeping the PI shape) while the loop rings.
        self.guard = OscillationGuard()
        self.reset()

    @property
    def gain_scale(self) -> float:
        """Factor the guard currently applies to ``kp`` (1 when the loop is calm)."""
        return self.guard.scale if self.guard.enabled else 1.0

    def reset(self) -> None:
        """Clear the integral and output state (call when tracking starts or stops)."""
        self.guard.reset()
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
        scale = self.guard.update((error_yaw, error_pitch), dt)
        kp, ki = g.kp * scale, g.ki * scale * scale
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
            if ki:
                # Anti-windup: the integral alone may never ask for more than full rate.
                limit = g.max_rate_deg_s / ki
                self._integral[axis] = max(-limit, min(limit, self._integral[axis]))
            rate = kp * error + ki * self._integral[axis] + feedforward[axis]
            rate = max(-g.max_rate_deg_s, min(g.max_rate_deg_s, rate))
            if g.max_accel_deg_s2 is not None and dt > 0:
                step = g.max_accel_deg_s2 * dt
                rate = max(self._rate[axis] - step, min(self._rate[axis] + step, rate))
            self._rate[axis] = rate
            out.append(self._to_units(axis, rate))
        return out[0], out[1]

    def _to_units(self, axis: int, rate: float) -> int:
        """Convert deg/s to whole 0x07 units, carrying the rounding into the next step.

        Three things stand between a wanted rate and the speed command that delivers it:

        - **The threshold.** The motor ignores speeds below ``min_units``. A slower
          wanted rate is sent as short pulses of the slowest speed it obeys, timed so the
          average is right; otherwise every fine correction would do nothing until the
          error grew enough for a big command, which then overshoots.
        - **The dead zone offset**, if the speed line doesn't pass through zero.
        - **Rounding.** One unit is about 0.7 deg/s, so plain rounding turns a wanted
          0.3 deg/s into nothing forever. The remainder is carried into the next step, so
          the average over a few steps is exact; the motor's lag smooths the pulses.
        """
        g = self.gains
        model = self.model
        calibrated = model.deg_per_unit[axis]
        per_unit = calibrated * self.turn_scale[axis]
        if abs(per_unit) <= 1e-3 or rate == 0.0:
            self._remainder[axis] = 0.0
            return 0
        deadzone = model.deadzone_units[axis]
        threshold = max(model.min_units[axis], 1.0)
        # Work in "effective" units: those that actually turn the gimbal.
        wanted = rate / per_unit + self._remainder[axis]
        sign = 1 if wanted > 0 else -1
        smallest = max(threshold - deadzone, 1e-3)  # effect of the slowest obeyed speed
        if abs(wanted) < smallest:
            # Below the slowest speed: a pulse when at least half of one is owed.
            units = round(threshold) if abs(wanted) >= smallest / 2 else 0
        else:
            units = min(g.max_speed, max(round(threshold), round(deadzone + abs(wanted))))
        delivered = abs(model.delivered(axis, units)) / abs(calibrated) if units else 0.0
        # Only carry what rounding lost; a saturated command keeps no debt.
        carry = abs(wanted) - delivered if units < g.max_speed else 0.0
        limit = max(1.0, smallest)
        self._remainder[axis] = sign * max(-limit, min(limit, carry))
        return sign * units

    def delivered_rate(
        self, command: tuple[int, int], *, scaled: bool = True
    ) -> tuple[float, float]:
        """The rate (deg/s) a speed command actually turns the gimbal (threshold included).

        ``scaled=False`` gives what the calibrated model alone predicts, without the
        learned ``turn_scale``.
        """
        rates = (self.model.delivered(0, command[0]), self.model.delivered(1, command[1]))
        if not scaled:
            return rates
        return rates[0] * self.turn_scale[0], rates[1] * self.turn_scale[1]
