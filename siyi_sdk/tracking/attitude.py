# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Timestamped gimbal attitude, so video measurements can be matched to the pose they saw."""

from __future__ import annotations

import bisect
import time
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from siyi_sdk.client import SIYIClient
    from siyi_sdk.models import GimbalAttitude


class AttitudeHistory:
    """Recent (time, yaw, pitch) samples on the ``time.monotonic()`` clock.

    Video frames arrive later than the attitude stream. Looking up the attitude
    at a frame's capture time tells you where the camera was pointing when the
    frame was taken, and the difference to the latest sample tells you how far
    it has turned since.
    """

    def __init__(self, seconds: float = 3.0) -> None:
        """Keep samples from the last ``seconds``."""
        self.seconds = seconds
        self.samples: deque[tuple[float, float, float]] = deque()

    def add(self, t: float, yaw: float, pitch: float) -> None:
        """Record a sample taken at monotonic time ``t``.

        Angles are unwrapped against the previous sample, so a gimbal reporting
        near ±180° (e.g. pitch when mounted inverted) moves smoothly instead of
        jumping 360°. Stored values may therefore leave the -180..180 range.
        """
        if self.samples:
            _, last_yaw, last_pitch = self.samples[-1]
            yaw += 360.0 * round((last_yaw - yaw) / 360.0)
            pitch += 360.0 * round((last_pitch - pitch) / 360.0)
        self.samples.append((t, yaw, pitch))
        while self.samples and self.samples[0][0] < t - self.seconds:
            self.samples.popleft()

    def attach(self, client: SIYIClient) -> None:
        """Record every attitude push from ``client`` (start the stream with 0x25 separately)."""

        def record(att: GimbalAttitude) -> None:
            self.add(time.monotonic(), att.yaw_deg, att.pitch_deg)

        client.on_attitude(record)

    def clear(self) -> None:
        """Forget all samples."""
        self.samples.clear()

    def latest(self) -> tuple[float, float, float] | None:
        """Newest (time, yaw, pitch), or None before the first sample."""
        return self.samples[-1] if self.samples else None

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
