# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Keep the gimbal pointed at a locked spot using the camera's own video."""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Literal

import numpy as np
from numpy.typing import NDArray

from siyi_sdk.tracking.attitude import AttitudeHistory
from siyi_sdk.tracking.control import (
    GimbalPredictor,
    LockGains,
    LoopModel,
    RateController,
    pixel_error_deg,
)
from siyi_sdk.tracking.estimator import TargetEstimator
from siyi_sdk.tracking.metrics import LockMetrics
from siyi_sdk.tracking.point_lock import PointLock, PointModel

if TYPE_CHECKING:
    from siyi_sdk.client import SIYIClient

SendRate = Callable[[int, int], Awaitable[None]]
SendAngle = Callable[[float, float], Awaitable[None]]
ControlMode = Literal["rate", "angle"]

# A8 Mini travel limits for 0x0E targets.
_YAW_LIMITS = (-135.0, 135.0)
_PITCH_LIMITS = (-90.0, 25.0)
# Attitude older than this is treated as missing (stream stopped).
_ATTITUDE_STALE_S = 0.3
# A control step this long after the previous one means the event loop stalled.
_STALL_S = 0.1
# Never extrapolate the target's motion more than this beyond the lead the measured delays
# call for (a frame that much later than expected is stale).
_MAX_EXTRA_LEAD_S = 0.3


class LockState(str, Enum):
    """Where a :class:`GimbalPointLock` is."""

    IDLE = "idle"  # nothing locked
    LOCKED = "locked"  # tracking and steering
    SEARCHING = "searching"  # scene motion lost; holding still until it recovers or times out


@dataclass(frozen=True)
class LockStatus:
    """Snapshot of the lock.

    Attributes:
        state: Current lock state.
        x: Locked spot in frame pixels (may be outside the frame), as of the last frame.
        y: Locked spot in frame pixels.
        width: Frame width the position refers to.
        height: Frame height the position refers to.
        score: Fraction of scene features agreeing with the motion estimate.
        error_deg: (yaw, pitch) pointing error the controller is acting on. With
            delay compensation this is the predicted error now, not the error
            the (older) frame shows.
        command: Last (yaw, pitch) 0x07 speeds sent; (0, 0) in angle mode.
        on_screen: Whether the spot is inside the frame.
        compensated: Whether attitude-based delay compensation is active.
        target_rate_deg_s: Estimated (yaw, pitch) angular velocity of the spot.
    """

    state: LockState
    x: float = 0.0
    y: float = 0.0
    width: int = 0
    height: int = 0
    score: float = 0.0
    error_deg: tuple[float, float] = (0.0, 0.0)
    command: tuple[int, int] = (0, 0)
    on_screen: bool = True
    compensated: bool = False
    target_rate_deg_s: tuple[float, float] = (0.0, 0.0)


class GimbalPointLock:
    """Lock onto a spot in the video and steer the gimbal to keep it centred.

    Feed every decoded frame to :meth:`update`. Each frame moves the locked spot
    with the scene (see :class:`PointLock`) and gives its direction at the
    moment the frame was captured.

    **With an attitude history** (recommended), a control loop runs at
    ``control_hz`` between frames. The spot's direction is fixed in gimbal
    angles using the attitude at capture time, its angular velocity is
    estimated across frames, and each control step predicts where the spot is
    *now* and compares that with the newest attitude sample. The video delay is
    then outside the feedback loop, so the loop can be several times faster
    without oscillating. Two ways to act on the prediction:

    - ``control="rate"``: PI on the predicted error plus velocity feedforward,
      sent as 0x07 speeds.
    - ``control="angle"``: send the predicted direction as a 0x0E angle target
      and let the gimbal's own position controller do the fast work. No gains
      to tune; best when the aircraft doesn't yaw quickly, because the A8 Mini
      reports yaw relative to the aircraft body.

    **Without an attitude history**, each frame drives a slower PI loop on the
    raw (delayed) image error.

    Use Lock gimbal mode so aircraft rotation is already stabilized. Measure the
    ``loop`` model with :func:`siyi_sdk.tracking.calibrate.calibrate_loop`.

    Example:
        >>> history = AttitudeHistory()
        >>> history.attach(client)  # and request the attitude stream at 50-100 Hz
        >>> lock = GimbalPointLock(client, attitude=history, loop=measured_model)
        >>> lock.lock(frame.frame, x=640, y=360)
        >>> await lock.update(next_frame.frame, timestamp=next_frame.timestamp)
        >>> await lock.release()
    """

    def __init__(
        self,
        client: SIYIClient | None = None,
        *,
        hfov_deg: float = 81.0,
        model: PointModel = "local",
        loop: LoopModel | None = None,
        gains: LockGains | None = None,
        attitude: AttitudeHistory | None = None,
        attitude_signs: tuple[int, int] = (1, 1),
        control: ControlMode = "rate",
        control_hz: float = 50.0,
        min_score: float = 0.2,
        lost_timeout: float = 2.0,
        trust_video_delay: bool = False,
        angle_pitch_offset: float = 0.0,
        smith: bool | None = None,
        send: SendRate | None = None,
        send_angle: SendAngle | None = None,
    ) -> None:
        """Create an idle lock.

        Args:
            client: Client for sending commands, unless ``send``/``send_angle`` are given.
            hfov_deg: Horizontal field of view at 1x zoom (A8 Mini spec: about 81).
            model: :class:`PointLock` motion model, ``local`` or ``global``.
            loop: Measured delays and turn rate; defaults to :class:`LoopModel`.
            gains: Controller gains; defaults to ``LockGains.for_model(loop)``.
            attitude: Attitude history fed by the attitude stream; enables delay
                compensation and the fast control loop.
            attitude_signs: Map from image directions (right, up) to the reported
                attitude: yaw_reported = sign * yaw_right.
            control: ``rate`` (0x07 speeds) or ``angle`` (0x0E targets; needs ``attitude``).
            control_hz: Control loop rate with an attitude history.
            min_score: Scores below this count as lost.
            lost_timeout: Seconds lost before the lock is released.
            trust_video_delay: Use ``loop.video_delay_s`` as given from the start
                (it was measured). Otherwise a shorter delay is used until the
                online estimate confirms it, since overestimating it is unsafe.
                Either way the estimate keeps adapting while locked.
            angle_pitch_offset: Reported pitch minus the 0x0E pitch that produces it:
                180 for an inverted mount (the A8 Mini then reports level as 180 but
                takes level as 0), else 0.
            smith: Steer against where the gimbal will be once the commands in flight
                take effect (:class:`GimbalPredictor`), which allows a higher gain. By
                default on for rate control with attitude and a measured motor lag.
            send: Coroutine taking (yaw, pitch) speeds; defaults to ``client.rotate_nowait``.
            send_angle: Coroutine taking (yaw, pitch) degrees in reported-attitude
                terms; defaults to ``client.set_attitude_nowait``.
        """
        if send is None and client is not None:
            send = client.rotate_nowait
        if send_angle is None and client is not None:
            send_angle = client.set_attitude_nowait
        if send is None:
            raise ValueError("pass a client or a send coroutine")
        if control == "angle" and (attitude is None or send_angle is None):
            raise ValueError("angle control needs an attitude history and a client or send_angle")
        self._send = send
        self._send_angle = send_angle
        self.hfov_deg = hfov_deg
        self.model: PointModel = model
        self.loop = loop or LoopModel()
        self.attitude = attitude
        self.signs = attitude_signs
        self.control: ControlMode = control
        self.control_hz = control_hz
        self.angle_pitch_offset = angle_pitch_offset
        if smith is None:
            smith = control == "rate" and attitude is not None and self.loop.motor_tau_s > 0
        self.predictor = (
            GimbalPredictor(self.loop.dead_time_s, self.loop.motor_tau_s) if smith else None
        )
        self.controller = RateController(
            gains
            or LockGains.for_model(self.loop, compensated=attitude is not None, smith=smith),
            self.loop,
        )
        self.min_score = min_score
        self.lost_timeout = lost_timeout
        self._tracker: PointLock | None = None
        self._last_time: float | None = None
        self._lost_since: float | None = None
        # (capture time, target yaw/pitch in image-aligned degrees, velocity deg/s)
        self._target: tuple[float, tuple[float, float], tuple[float, float]] | None = None
        # The spot's direction and angular velocity, filtered across frames
        # (siyi_sdk.tracking.estimator). Attitude is relative to the mount, so turning
        # the mount (by hand, or an aircraft yawing) moves the spot at the turn rate;
        # far beyond 180 deg/s is measurement error.
        self.estimator = TargetEstimator(max_rate_deg_s=180.0)
        # Measurement noise: the tracker's pixel jitter, plus the error of the attitude
        # interpolated at an estimated capture time.
        self.tracker_noise_px = 1.0
        self.attitude_noise_deg = 0.05
        self._sigma_deg = 0.1
        # Online video-delay estimate: (arrival time, image error yaw, pitch) per frame.
        self.adapt_delay = True
        self._frames: deque[tuple[float, float, float]] = deque()
        self._frames_since_fit = 0
        # Until the online estimate has confirmed the delay, use a shorter one:
        # overestimating the delay destabilizes the loop, underestimating only
        # slows it slightly.
        self._delay_confirmed = trust_video_delay
        self._task: asyncio.Task[None] | None = None
        self._last_command: tuple[int, int] | None = None
        self.status = LockStatus(LockState.IDLE)
        # Scores the current (or last) lock; see siyi_sdk.tracking.metrics.
        self.metrics = LockMetrics()

    @property
    def tracker(self) -> PointLock | None:
        """The fixed-scene tracker while locked, for drawing its debug view."""
        return self._tracker

    @property
    def active(self) -> bool:
        """Whether a spot is locked (tracking or searching)."""
        return self._tracker is not None

    @property
    def compensated(self) -> bool:
        """Whether the fast, delay-compensated loop is in use."""
        return self.attitude is not None

    def lock(self, frame: NDArray[np.uint8], x: float, y: float, size: int = 60) -> None:
        """Lock onto pixel (x, y) of ``frame``; ``size`` only sets the drawn box."""
        tracker = PointLock(self.model)
        tracker.init(frame, (round(x - size / 2), round(y - size / 2), size, size))
        self._tracker = tracker
        self.controller.reset()
        if self.predictor is not None:
            self.predictor.reset()
        self._last_time = None
        self._lost_since = None
        self._target = None
        self.estimator.reset()
        self._frames.clear()
        self._last_command = None
        self.metrics = LockMetrics()
        height, width = frame.shape[:2]
        self.status = LockStatus(
            LockState.LOCKED, x, y, width, height, 1.0, compensated=self.compensated
        )

    def _flip(self, yaw: float, pitch: float) -> tuple[float, float]:
        """Convert between image-aligned angles and reported attitude (signs are self-inverse)."""
        return yaw * self.signs[0], pitch * self.signs[1]

    async def update(
        self, frame: NDArray[np.uint8], *, zoom: float = 1.0, timestamp: float | None = None
    ) -> LockStatus:
        """Track into ``frame`` and return the new status.

        ``timestamp`` is the frame's arrival time on the ``time.monotonic()``
        clock (``StreamFrame.timestamp``); the capture time is that minus
        ``loop.video_delay_s``.
        """
        tracker = self._tracker
        if tracker is None:
            return self.status
        now = time.monotonic() if timestamp is None else timestamp
        dt = 0.0 if self._last_time is None else now - self._last_time
        self._last_time = now
        # Optical flow takes a few milliseconds; keep it off the event loop.
        await asyncio.to_thread(tracker.update, frame)
        if self._tracker is not tracker:  # released or re-locked meanwhile
            return self.status
        height, width = frame.shape[:2]
        x, y = tracker.center()
        score = tracker.getTrackingScore()
        error = pixel_error_deg(x, y, width, height, hfov_deg=self.hfov_deg, zoom=zoom)
        self._sigma_deg = math.hypot(
            TargetEstimator.pixel_sigma_deg(self.tracker_noise_px, width, self.hfov_deg, zoom),
            self.attitude_noise_deg,
        )
        on_screen = 0 <= x < width and 0 <= y < height

        if score < self.min_score:
            self._lost_since = self._lost_since if self._lost_since is not None else now
            if now - self._lost_since > self.lost_timeout:
                await self.release()
                return self.status
            self.controller.reset()
            self._target = None
            self.estimator.reset()
            await self._command((0, 0))
            self.status = LockStatus(
                LockState.SEARCHING,
                x,
                y,
                width,
                height,
                score,
                error,
                (0, 0),
                on_screen,
                self.compensated,
            )
            return self.status
        self._lost_since = None

        if self.attitude is not None and self.adapt_delay:
            self._frames.append((now, error[0], error[1]))
            while self._frames and self._frames[0][0] < now - 2.0:
                self._frames.popleft()
            self._frames_since_fit += 1
            if self._frames_since_fit >= 5:
                self._frames_since_fit = 0
                self._refine_video_delay()
        if self.attitude is not None and self._observe(error, now):
            self._ensure_loop()
            prev = self.status
            self.status = LockStatus(
                LockState.LOCKED,
                x,
                y,
                width,
                height,
                score,
                prev.error_deg,
                prev.command,
                on_screen,
                True,
                self._target[2] if self._target else (0.0, 0.0),
            )
            return self.status

        # No usable attitude: steer on the raw image error once per frame.
        command = self.controller.update(*error, dt)
        await self._command(command)
        self.metrics.add(now, error, command)
        self.status = LockStatus(
            LockState.LOCKED, x, y, width, height, score, error, command, on_screen, False
        )
        return self.status

    def _observe(self, error: tuple[float, float], arrival: float) -> bool:
        """Fix the spot's direction in gimbal angles at capture time; False if no attitude."""
        assert self.attitude is not None
        latest = self.attitude.latest()
        if latest is None or time.monotonic() - latest[0] > _ATTITUDE_STALE_S:
            return False
        delay = self.loop.video_delay_s * (1.0 if self._delay_confirmed else 0.8)
        captured = arrival - delay
        pose = self.attitude.at(captured)
        if pose is None:
            return False
        yaw, pitch = self._flip(*pose)
        # The filter de-noises the position and estimates the velocity the controller
        # extrapolates across the delays (frame-to-frame differences are far noisier).
        position, velocity = self.estimator.update(
            captured, (yaw + error[0], pitch + error[1]), self._sigma_deg
        )
        self._target = (captured, position, velocity)
        return True

    def _refine_video_delay(self) -> None:
        """Nudge ``loop.video_delay_s`` toward the delay that best explains recent frames.

        With the right delay, the spot's direction (attitude at capture time
        plus image error) follows a straight line in time; with a wrong one the
        gimbal's own turning leaks into it. Only frames where the gimbal turned
        enough to tell delays apart are used.
        """
        assert self.attitude is not None
        if len(self._frames) < 12 or len(self.attitude.samples) < 10:
            return
        frames = np.array(self._frames)
        samples = np.array(self.attitude.samples)
        arrival = frames[:, 0]
        current = self.loop.video_delay_s
        candidates = np.clip(np.arange(current - 0.06, current + 0.0601, 0.005), 0.0, 1.0)
        best_delay, best_cost, worst_cost = current, np.inf, 0.0
        for delay in candidates:
            captured = arrival - delay
            if captured[0] < samples[0, 0]:
                continue  # attitude history doesn't reach back that far
            cost = 0.0
            for axis, sign in ((1, self.signs[0]), (2, self.signs[1])):
                target = np.interp(captured, samples[:, 0], samples[:, axis]) * sign
                target = target + frames[:, axis]
                fit = np.polyval(np.polyfit(captured, target, 1), captured)
                cost += float(np.mean((target - fit) ** 2))
            worst_cost = max(worst_cost, cost)
            if cost < best_cost:
                best_delay, best_cost = float(delay), cost
        # Without enough gimbal motion every delay fits equally well: keep the estimate.
        if not np.isfinite(best_cost) or worst_cost < 4 * best_cost + 0.05:
            return
        self.loop.video_delay_s = 0.6 * current + 0.4 * best_delay
        self._delay_confirmed = True

    def _ensure_loop(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._control_loop())

    async def _control_loop(self) -> None:
        period = 1.0 / self.control_hz
        last = time.monotonic()
        while self._tracker is not None:
            await asyncio.sleep(period)
            now = time.monotonic()
            dt, last = now - last, now
            await self._control_step(now, dt)

    async def _control_step(self, now: float, dt: float) -> None:
        """One fast step: predict the spot's direction now and steer toward it."""
        if self.attitude is None or self._target is None or self._lost_since is not None:
            return
        latest = self.attitude.latest()
        if latest is None or now - latest[0] > _ATTITUDE_STALE_S:
            await self._command((0, 0))
            return
        captured, target, velocity = self._target
        # Bring the frame's measurement up to now. An angle target is reached a
        # command delay later, so aim it that far ahead; a speed loop compares
        # against the current attitude, and at steady speed the command delay
        # doesn't move the camera, so leading it would only add a bias.
        lead = now - captured
        expected = self.loop.video_delay_s
        if self.control == "angle":
            lead += self.loop.command_delay_s
            expected += self.loop.command_delay_s
        elif self.predictor is not None:
            # Compare where both will be once the commands in flight have acted.
            lead += self.predictor.dead_time_s
            expected += self.predictor.dead_time_s
        # Further than this the frame is stale (video or the loop stalled), and a velocity
        # carried that far would steer on a guess.
        lead = min(lead, expected + _MAX_EXTRA_LEAD_S)
        predicted = (target[0] + velocity[0] * lead, target[1] + velocity[1] * lead)
        current = self._flip(latest[1], latest[2])
        if self.control == "rate" and self.predictor is not None:
            ahead = self.predictor.turn_ahead(now)
            current = (current[0] + ahead[0], current[1] + ahead[1])
        error = (predicted[0] - current[0], predicted[1] - current[1])
        if self.control == "angle":
            assert self._send_angle is not None
            yaw, pitch = self._flip(*predicted)
            # Attitude history is unwrapped and in reported terms; 0x0E wants -180..180
            # in command terms.
            pitch = (pitch - self.angle_pitch_offset + 180.0) % 360.0 - 180.0
            yaw = min(max(yaw, _YAW_LIMITS[0]), _YAW_LIMITS[1])
            pitch = min(max(pitch, _PITCH_LIMITS[0]), _PITCH_LIMITS[1])
            await self._send_angle(yaw, pitch)
            command = (0, 0)
        else:
            # After a stall, steer on the fresh error but don't let the gap wind up the integral.
            command = self.controller.update(
                *error, dt, feedforward=velocity, integrate=dt <= _STALL_S
            )
            await self._command(command)
            if self.predictor is not None:
                per_unit = self.loop.deg_per_unit
                self.predictor.record(now, (command[0] * per_unit[0], command[1] * per_unit[1]))
        self.metrics.add(now, error, command)
        s = self.status
        self.status = LockStatus(
            s.state,
            s.x,
            s.y,
            s.width,
            s.height,
            s.score,
            error,
            command,
            s.on_screen,
            True,
            velocity,
        )

    async def _command(self, command: tuple[int, int]) -> None:
        # Repeated stops add nothing; anything else is resent every step.
        if command == (0, 0) and self._last_command == (0, 0):
            return
        if self.control == "angle" and self.compensated and command == (0, 0):
            # Angle mode holds the last target; a speed command would fight it.
            self._last_command = command
            return
        self._last_command = command
        await self._send(*command)

    async def release(self) -> None:
        """Stop tracking and stop the gimbal."""
        was_active = self._tracker is not None
        self._tracker = None
        self._target = None
        self.estimator.reset()
        self._frames.clear()
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.controller.reset()
        self.status = LockStatus(LockState.IDLE)
        if was_active and self.control == "rate":
            await self._send(0, 0)
