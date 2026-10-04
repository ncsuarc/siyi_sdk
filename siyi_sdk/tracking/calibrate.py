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
    command_delay_s: float
    video_delay_s: float
    image_scale: float  # picture rotation / attitude rotation (sign = attitude convention)
    fit_error_deg: float  # RMS mismatch of the delay fit


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
    return AxisResult(slope / speed, max(0.0, onset - t_command), lag, scale, rms)


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
) -> LoopCalibration:
    """Measure both axes and return a :class:`LoopModel` plus signs and field of view.

    Needs the attitude stream running (50-100 Hz is best) into ``attitude``,
    and every decoded frame passed to ``recorder.add``. Point the camera at a
    textured, static scene; plain sky or water gives no picture motion.
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
    model = LoopModel(
        # The controller works in image-aligned terms, so fold the attitude sign in.
        deg_per_unit=(
            yaw.deg_per_unit_attitude * yaw_sign,
            (pitch_result.deg_per_unit_attitude * pitch_sign)
            if pitch_result
            else yaw.deg_per_unit_attitude * yaw_sign,
        ),
        command_delay_s=float(np.median([r.command_delay_s for r in results])),
        video_delay_s=float(np.median([r.video_delay_s for r in results])),
    )
    for r, name in ((yaw, "yaw"), (pitch_result, "pitch")):
        if r is not None and r.fit_error_deg > 0.5:
            notes.append(
                f"{name}: picture and attitude matched poorly ({r.fit_error_deg:.2f} deg RMS); "
                "use a textured, static scene"
            )
    return LoopCalibration(model, (yaw_sign, pitch_sign), corrected, yaw, pitch_result, notes)
