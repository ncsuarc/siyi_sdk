"""Pixel-to-angle math for pointing the gimbal from the video view.

Screen positions are normalized offsets from the image center: ``x`` is -0.5
at the left edge and +0.5 at the right, ``y`` is -0.5 at the top and +0.5 at
the bottom. Angles are degrees in a frame where positive yaw turns right and
positive pitch looks up; ``yaw_sign``/``pitch_sign`` map that onto the
camera's reported attitude.

The lens is treated as rectilinear (pinhole) and roll as zero, which the
stabilized gimbal holds. A8 Mini digital zoom narrows the field of view as
``tan(fov / 2) / zoom``.
"""

from __future__ import annotations

import bisect
import math
from collections import deque
from dataclasses import dataclass

# A8 Mini limits from protocol commands 0x0E and 0x41.
YAW_LIMITS = (-135.0, 135.0)
PITCH_LIMITS = (-90.0, 25.0)


@dataclass
class PointingConfig:
    """Lens and timing settings the operator can calibrate."""

    # A8 Mini spec sheet: 81 deg horizontal, 93 deg diagonal. Calibrate on hardware.
    hfov_deg: float = 81.0
    # Protocol 0x0D says attitude is NED: yaw grows to the right, pitch grows upward.
    yaw_sign: int = 1
    pitch_sign: int = 1
    # Time between the camera seeing a scene and the browser showing it.
    video_delay_ms: float = 200.0
    # Point lock: rotation speed units per degree of error, top speed, motion model.
    lock_gain: float = 2.0
    lock_max_speed: int = 60
    lock_model: str = "local"


def _ray(x: float, y: float, aspect: float, zoom: float, hfov_deg: float) -> tuple[float, float]:
    """Return (right, up) components of the camera ray at a screen position, forward = 1."""
    tan_h = math.tan(math.radians(hfov_deg) / 2) / zoom
    return 2 * x * tan_h, -2 * y * tan_h / aspect


def _direction(yaw: float, pitch: float, right: float, up: float) -> tuple[float, float]:
    """World yaw/pitch of a camera ray when the camera center points at (yaw, pitch)."""
    p = math.radians(pitch)
    forward_h = math.cos(p) - up * math.sin(p)
    vertical = math.sin(p) + up * math.cos(p)
    return (
        yaw + math.degrees(math.atan2(right, forward_h)),
        math.degrees(math.atan2(vertical, math.hypot(forward_h, right))),
    )


def screen_to_world(yaw: float, pitch: float, x: float, y: float, *, aspect: float,
                    zoom: float, hfov_deg: float) -> tuple[float, float]:
    """World yaw/pitch of the scene point shown at (x, y)."""
    return _direction(yaw, pitch, *_ray(x, y, aspect, zoom, hfov_deg))


def center_for(target_yaw: float, target_pitch: float, x: float, y: float, *, aspect: float,
               zoom: float, hfov_deg: float,
               pitch_hint: float | None = None) -> tuple[float, float]:
    """Camera attitude that shows the world direction (target_yaw, target_pitch) at (x, y).

    Looking steeply down, two camera pitches can show the same point at the
    same off-center pixel; the solver returns the one nearest ``pitch_hint``.
    """
    right, up = _ray(x, y, aspect, zoom, hfov_deg)

    def elevation(pitch: float) -> float:
        return _direction(0.0, pitch, right, up)[1]

    # The ray's elevation depends on camera pitch alone; solve that with Newton
    # steps, then yaw is a plain offset. Near vertical the elevation changes
    # slowly with pitch, so fixed-point iteration would crawl there.
    pitch = target_pitch - math.degrees(math.atan(up)) if pitch_hint is None else pitch_hint
    for _ in range(20):
        error = elevation(pitch) - target_pitch
        if abs(error) < 1e-6:
            break
        slope = (elevation(pitch + 1e-3) - elevation(pitch - 1e-3)) / 2e-3
        pitch -= error / slope if abs(slope) > 1e-3 else error
    return target_yaw - (_direction(0.0, pitch, right, up)[0]), pitch


def clamp_attitude(yaw: float, pitch: float) -> tuple[float, float]:
    """Limit a target to the A8 Mini's yaw and pitch travel."""
    return (
        min(max(yaw, YAW_LIMITS[0]), YAW_LIMITS[1]),
        min(max(pitch, PITCH_LIMITS[0]), PITCH_LIMITS[1]),
    )


class AttitudeHistory:
    """Recent attitude samples, so a click can use the pose its video frame showed."""

    def __init__(self, seconds: float = 3.0) -> None:
        """Keep samples from the last ``seconds``."""
        self.seconds = seconds
        self.samples: deque[tuple[float, float, float]] = deque()

    def add(self, t: float, yaw: float, pitch: float) -> None:
        """Record a sample taken at monotonic time ``t``."""
        self.samples.append((t, yaw, pitch))
        while self.samples and self.samples[0][0] < t - self.seconds:
            self.samples.popleft()

    def clear(self) -> None:
        """Forget all samples."""
        self.samples.clear()

    def at(self, t: float) -> tuple[float, float] | None:
        """Linearly interpolated (yaw, pitch) at time t, clamped to the stored range."""
        if not self.samples:
            return None
        times = [s[0] for s in self.samples]
        i = bisect.bisect_left(times, t)
        if i == 0:
            return self.samples[0][1:]
        if i == len(times):
            return self.samples[-1][1:]
        (t0, y0, p0), (t1, y1, p1) = self.samples[i - 1], self.samples[i]
        k = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
        return y0 + (y1 - y0) * k, p0 + (p1 - p0) * k
