# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Measure the point-lock loop: turn rate, command delay, video delay, signs and field of view.

The gimbal is turned at a fixed speed on one axis and back while two things
are recorded: the attitude stream, and how far the video picture shifts
between frames (phase correlation). From these:

- the slope of the attitude while turning gives degrees per second per 0x07 unit;
- where that slope meets the starting attitude gives the command delay;
- the time shift that best lines up picture motion with attitude gives the
  video delay;
- the sign and scale of that fit give the attitude direction convention and a
  corrected field of view.

The gimbal moves about ``speed * deg_per_unit * move_s`` degrees each way.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from siyi_sdk.tracking.attitude import AttitudeHistory
from siyi_sdk.tracking.control import LoopModel

SendRate = Callable[[int, int], Awaitable[None]]


class CalibrationError(RuntimeError):
    """The measurement could not be completed (no attitude, no video, or no motion)."""


class FrameMotionRecorder:
    """Accumulate how far the picture has shifted, in pixels, frame by frame.

    Call :meth:`add` from the frame callback with every decoded frame. Only
    frames added while :attr:`recording` is True are measured.
    """

    WORK_WIDTH = 320

    def __init__(self) -> None:
        """Create an idle recorder."""
        self.recording = False
        self.samples: list[tuple[float, float, float]] = []  # (arrival time, cum dx, cum dy)
        self.width = 0
        self._previous: NDArray[Any] | None = None
        self._window: NDArray[Any] | None = None
        self._scale = 1.0

    def start(self) -> None:
        """Begin a new recording."""
        self.samples = []
        self._previous = None
        self.recording = True

    def stop(self) -> None:
        """Stop recording."""
        self.recording = False

    def add(self, image: NDArray[Any], timestamp: float) -> None:
        """Measure the shift from the previous frame (call once per decoded frame)."""
        if not self.recording:
            return
        self.width = image.shape[1]
        self._scale = min(1.0, self.WORK_WIDTH / image.shape[1])
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        size = (round(gray.shape[1] * self._scale), round(gray.shape[0] * self._scale))
        small = cv2.resize(gray, size, interpolation=cv2.INTER_AREA).astype(np.float32)
        if self._window is None or self._window.shape != small.shape:
            self._window = cv2.createHanningWindow(size, cv2.CV_32F)
        if self._previous is None or self._previous.shape != small.shape:
            self.samples.append((timestamp, 0.0, 0.0))
        else:
            (dx, dy), _ = cv2.phaseCorrelate(self._previous, small, self._window)
            _, x0, y0 = self.samples[-1]
            self.samples.append((timestamp, x0 + dx / self._scale, y0 + dy / self._scale))
        self._previous = small


@dataclass
class AxisResult:
    """Measurements for one axis."""

    deg_per_unit_attitude: float  # signed, in reported-attitude terms
    command_delay_s: float  # dead time plus motor lag: where the steady turn extrapolates to 0
    video_delay_s: float
    image_scale: float  # picture rotation / attitude rotation (sign = attitude convention)
    fit_error_deg: float  # RMS mismatch of the delay fit
    motor_tau_s: float = 0.0  # first-order lag of the motor getting up to speed


@dataclass
class LoopCalibration:
    """Result of :func:`calibrate_loop`."""

    model: LoopModel
    attitude_signs: tuple[int, int]
    hfov_deg: float  # field of view corrected by the yaw measurement
    yaw: AxisResult
    pitch: AxisResult | None
    notes: list[str] = field(default_factory=list)


def _pixels_to_deg(pixels: NDArray[Any], width: int, hfov_deg: float) -> NDArray[Any]:
    """Convert accumulated picture shift to rotation.

    Each frame-to-frame shift is measured near the image centre, where a small
    rotation moves the picture by ``focal * angle``; summing them stays linear
    in the total rotation (unlike the position of one point, which goes as tan).
    """
    focal = (width / 2) / math.tan(math.radians(hfov_deg) / 2)
    return np.degrees(pixels / focal)


def _onset_and_rate(
    times: NDArray[Any], values: NDArray[Any], t_command: float, t_stop: float
) -> tuple[float, float]:
    """Fit the turning segment; return (onset time, slope in deg/s)."""
    baseline = float(np.median(values[times < t_command])) if np.any(times < t_command) else 0.0
    moving = (times > t_command) & (times <= t_stop)
    t, v = times[moving], values[moving] - baseline
    if len(t) < 5:
        raise CalibrationError("too few attitude samples while turning; is the stream running?")
    span = v[-1]
    if abs(span) < 1.0:
        raise CalibrationError("the gimbal barely moved; check Lock mode and the speed")
    # Fit only the steady part of the turn (ignore the start-up ramp).
    steady = (np.abs(v) >= 0.3 * abs(span)) & (np.abs(v) <= 0.95 * abs(span))
    if steady.sum() < 3:
        steady = np.abs(v) >= 0.3 * abs(span)
    slope, intercept = np.polyfit(t[steady], v[steady], 1)
    onset = -intercept / slope
    return float(onset), float(slope)


def _motor_lag(
    times: NDArray[Any],
    values: NDArray[Any],
    t_command: float,
    t_stop: float,
    onset: float,
    slope: float,
) -> float:
    """Split the onset into dead time and a first-order motor lag; return the lag.

    After dead time ``d`` a first-order motor with lag ``tau`` turns through
    ``slope * (x - tau * (1 - exp(-x / tau)))`` at ``x = t - d``; its steady part
    extrapolates to zero at ``d + tau``, which is ``onset``. So only ``tau`` is
    free: pick the one that best fits the start of the turn.
    """
    baseline = float(np.median(values[times < t_command])) if np.any(times < t_command) else 0.0
    use = (times > t_command) & (times <= t_stop)
    t, v = times[use], values[use] - baseline
    if len(t) < 5 or onset <= t_command:
        return 0.0
    best_tau, best_cost = 0.0, np.inf
    for tau in np.arange(0.0, onset - t_command, 0.002):
        x = np.maximum(t - (onset - tau), 0.0)
        ramp = x - (tau * (1 - np.exp(-x / tau)) if tau > 0 else 0.0)
        cost = float(np.mean((slope * ramp - v) ** 2))
        if cost < best_cost:
            best_tau, best_cost = float(tau), cost
    return best_tau


def _video_delay(
    att_t: NDArray[Any], att_v: NDArray[Any], img_t: NDArray[Any], img_v: NDArray[Any]
) -> tuple[float, float, float]:
    """Find lag L and scale k with img(t) ~ k * att(t - L) + b; return (L, k, rms)."""
    best = (0.0, 1.0, math.inf)
    for lag in np.arange(0.0, 0.8, 0.005):
        att = np.interp(img_t - lag, att_t, att_v)
        a = np.vstack([att, np.ones_like(att)]).T
        (k, b), *_ = np.linalg.lstsq(a, img_v, rcond=None)
        rms = float(np.sqrt(np.mean((a @ np.array([k, b]) - img_v) ** 2)))
        if rms < best[2]:
            best = (float(lag), float(k), rms)
    return best


async def _turn(send: SendRate, yaw: int, pitch: int, seconds: float) -> None:
    """Hold a speed command, resending at 20 Hz so watchdogs don't stop it."""
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            await send(yaw, pitch)
            await asyncio.sleep(0.05)
    finally:  # also on cancellation: never leave the gimbal turning
        for _ in range(3):
            await send(0, 0)


async def measure_axis(
    send: SendRate,
    attitude: AttitudeHistory,
    recorder: FrameMotionRecorder,
    *,
    axis: str,
    hfov_deg: float,
    zoom: float = 1.0,
    speed: int = 25,
    move_s: float = 0.8,
    settle_s: float = 0.8,
) -> AxisResult:
    """Turn one axis out and back while recording; see the module docstring."""
    index = 0 if axis == "yaw" else 1
    command = (speed, 0) if index == 0 else (0, speed)
    # The history normally keeps a few seconds; the whole run must stay in it.
    kept = attitude.seconds
    attitude.seconds = max(kept, 2 * (move_s + settle_s) + 5.0)
    recorder.start()
    try:
        await asyncio.sleep(0.3)  # baseline
        t_command = time.monotonic()
        await _turn(send, *command, move_s)
        t_stop = time.monotonic()
        await asyncio.sleep(settle_s)
        await _turn(send, -command[0], -command[1], move_s)  # come back
        await asyncio.sleep(settle_s)
    finally:
        recorder.stop()
        attitude.seconds = kept

    samples = [s for s in attitude.samples if s[0] >= t_command - 0.3]
    if len(samples) < 10:
        raise CalibrationError("no attitude stream; request it at 50 Hz or more first")
    att_t = np.array([s[0] for s in samples])
    att_v = np.array([s[1 + index] for s in samples])
    onset, slope = _onset_and_rate(att_t, att_v, t_command, t_stop)
    tau = _motor_lag(att_t, att_v, t_command, t_stop, onset, slope)

    frames = recorder.samples
    if len(frames) < 10:
        raise CalibrationError("no video frames were recorded during the move")
    img_t = np.array([f[0] for f in frames])
    shift = np.array([f[1 + index] for f in frames])
    width = recorder.width
    focal_hfov = math.degrees(2 * math.atan(math.tan(math.radians(hfov_deg) / 2) / zoom))
    # Turning right/up moves the picture left/down: image-aligned rotation is -dx / +dy.
    image_deg = _pixels_to_deg(-shift if index == 0 else shift, width, focal_hfov)
    lag, scale, rms = _video_delay(att_t, att_v, img_t, image_deg)
    return AxisResult(slope / speed, max(0.0, onset - t_command), lag, scale, rms, tau)


async def measure_threshold(
    send: SendRate,
    attitude: AttitudeHistory,
    *,
    axis: str,
    deg_per_unit: float,
    speeds: tuple[int, ...] = (2, 3, 4, 5, 6, 7, 8, 10),
    hold_s: float = 0.4,
) -> float:
    """Find the slowest 0x07 speed that turns ``axis`` at all; 0 if the slowest tried does.

    The A8 Mini ignores small speeds outright (a threshold, not an offset of the speed
    line), which a fit through two faster speeds can't see. Each speed is held briefly,
    then reversed so the gimbal ends where it started. ``deg_per_unit`` is the rate
    measured at a normal speed (attitude terms, any sign).
    """
    index = 0 if axis == "yaw" else 1
    for speed in speeds:
        command = (speed, 0) if index == 0 else (0, speed)
        start = time.monotonic()
        await _turn(send, *command, hold_s)
        end = time.monotonic()
        samples = [s for s in attitude.samples if start + hold_s / 2 <= s[0] <= end + 0.05]
        moved = False
        if len(samples) >= 3:
            span = samples[-1][0] - samples[0][0]
            rate = (samples[-1][1 + index] - samples[0][1 + index]) / span
            moved = abs(rate) >= 0.5 * abs(deg_per_unit) * speed
        await asyncio.sleep(0.2)
        if moved:
            await _turn(send, -command[0], -command[1], hold_s)  # undo the nudge
            await asyncio.sleep(0.2)
            return 0.0 if speed == speeds[0] else float(speed)
    return float(speeds[-1])


async def calibrate_loop(
    send: SendRate,
    attitude: AttitudeHistory,
    recorder: FrameMotionRecorder,
    *,
    hfov_deg: float,
    zoom: float = 1.0,
    speed: int = 25,
    move_s: float = 0.8,
    pitch: bool = True,
    low_speed: int | None = 10,
) -> LoopCalibration:
    """Measure both axes and return a :class:`LoopModel` plus signs and field of view.

    Needs the attitude stream running (50-100 Hz is best) into ``attitude``,
    and every decoded frame passed to ``recorder.add``. Point the camera at a
    textured, static scene; plain sky or water gives no picture motion.

    With ``low_speed``, each axis also turns at that slower speed. The two turn rates
    give the motor's dead zone (the slow speeds it ignores) and its true rate per unit
    above it; one speed alone can't tell them apart.
    """
    yaw = await measure_axis(
        send,
        attitude,
        recorder,
        axis="yaw",
        hfov_deg=hfov_deg,
        zoom=zoom,
        speed=speed,
        move_s=move_s,
    )
    pitch_result = None
    if pitch:
        pitch_result = await measure_axis(
            send,
            attitude,
            recorder,
            axis="pitch",
            hfov_deg=hfov_deg,
            zoom=zoom,
            speed=speed,
            move_s=move_s,
        )
    notes: list[str] = []
    yaw_sign = 1 if yaw.image_scale >= 0 else -1
    pitch_sign = 1 if pitch_result is None or pitch_result.image_scale >= 0 else -1
    # The picture moved |k| degrees per degree of attitude; correct the field of view to match.
    k = abs(yaw.image_scale)
    corrected = hfov_deg
    if 0.5 < k < 2.0:
        corrected = math.degrees(2 * math.atan(math.tan(math.radians(hfov_deg) / 2) / k))
    else:
        notes.append(f"picture/attitude scale {k:.2f} is implausible; field of view left unchanged")
    results = [r for r in (yaw, pitch_result) if r is not None]
    per_unit = {"yaw": yaw.deg_per_unit_attitude, "pitch": None}
    deadzone = {"yaw": 0.0, "pitch": 0.0}
    if pitch_result is not None:
        per_unit["pitch"] = pitch_result.deg_per_unit_attitude
    for name, fast in (("yaw", yaw), ("pitch", pitch_result)):
        if fast is None or not low_speed or low_speed >= speed:
            continue
        try:
            slow = await measure_axis(
                send,
                attitude,
                recorder,
                axis=name,
                hfov_deg=hfov_deg,
                zoom=zoom,
                speed=low_speed,
                move_s=move_s,
            )
        except CalibrationError as exc:
            notes.append(f"{name}: slow turn failed ({exc}); dead zone not measured")
            continue
        fast_rate = fast.deg_per_unit_attitude * speed
        slow_rate = slow.deg_per_unit_attitude * low_speed
        slope = (fast_rate - slow_rate) / (speed - low_speed)
        if slope * fast_rate <= 0 or abs(slow_rate) > abs(fast_rate):
            notes.append(f"{name}: slow and fast turns disagree; dead zone not measured")
            continue
        zone = low_speed - slow_rate / slope
        deadzone[name] = float(min(max(zone, 0.0), low_speed - 1))
        per_unit[name] = fast_rate / (speed - deadzone[name])
    threshold = {"yaw": 0.0, "pitch": 0.0}
    for name, fast in (("yaw", yaw), ("pitch", pitch_result)):
        if fast is not None and low_speed:
            threshold[name] = await measure_threshold(
                send, attitude, axis=name, deg_per_unit=fast.deg_per_unit_attitude
            )
    yaw_per_unit = per_unit["yaw"]
    assert yaw_per_unit is not None
    yaw_rate = yaw_per_unit * yaw_sign
    pitch_per_unit = per_unit["pitch"]
    pitch_rate = pitch_per_unit * pitch_sign if pitch_per_unit is not None else yaw_rate
    model = LoopModel(
        # The controller works in image-aligned terms, so fold the attitude sign in.
        deg_per_unit=(yaw_rate, pitch_rate),
        command_delay_s=float(np.median([r.command_delay_s for r in results])),
        video_delay_s=float(np.median([r.video_delay_s for r in results])),
        motor_tau_s=float(np.median([r.motor_tau_s for r in results])),
        deadzone_units=(
            deadzone["yaw"],
            deadzone["pitch"] if pitch_result is not None else deadzone["yaw"],
        ),
        min_units=(
            threshold["yaw"],
            threshold["pitch"] if pitch_result is not None else threshold["yaw"],
        ),
    )
    for r, name in ((yaw, "yaw"), (pitch_result, "pitch")):
        if r is not None and r.fit_error_deg > 0.5:
            notes.append(
                f"{name}: picture and attitude matched poorly ({r.fit_error_deg:.2f} deg RMS); "
                "use a textured, static scene"
            )
    return LoopCalibration(model, (yaw_sign, pitch_sign), corrected, yaw, pitch_result, notes)
