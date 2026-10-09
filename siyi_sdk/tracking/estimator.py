"""Estimate where a locked spot is and how fast it moves, from noisy, irregular frames.

A constant-velocity Kalman filter per axis (yaw, pitch), in the image-aligned gimbal-angle
degrees :class:`~siyi_sdk.tracking.gimbal.GimbalPointLock` fixes the spot in. Compared with
fitting a line to the last fraction of a second it:

- **reacts at once:** each frame corrects the estimate by how surprising it is, weighted by
  how much the filter trusts its prediction, instead of waiting for a window to fill;
- **smooths noise:** small tracker jitter is averaged out by the same weighting;
- **uses real capture times,** so late or bunched frames don't distort the velocity;
- **rejects glitches:** one measurement far outside what the filter expects is ignored. A
  second one in a row on the same side means the motion really changed (the rig was
  swung, the target sped off): the filter restarts from those two frames, so a genuine
  change costs at most one extra frame.

Pure Python (2x2 per axis), no NumPy, so it is cheap at any frame rate.
"""

from __future__ import annotations

import math


class _Axis:
    """Position/velocity filter for one angle."""

    def __init__(self) -> None:
        self.x = [0.0, 0.0]  # angle (deg), rate (deg/s)
        self.p = [[0.0, 0.0], [0.0, 0.0]]

    def start(self, z: float, r: float, rate: float, rate_var: float) -> None:
        self.x = [z, rate]
        self.p = [[r, 0.0], [0.0, rate_var]]

    def predict(self, dt: float, q: float) -> None:
        if dt <= 0:
            return
        (p00, p01), (p10, p11) = self.p
        self.x = [self.x[0] + self.x[1] * dt, self.x[1]]
        # F P F' + Q for white-noise acceleration of spectral density q.
        p00 = p00 + dt * (p10 + p01) + dt * dt * p11 + q * dt**3 / 3
        p01 = p01 + dt * p11 + q * dt * dt / 2
        p11 = p11 + q * dt
        self.p = [[p00, p01], [p01, p11]]

    def innovation(self, z: float, r: float) -> tuple[float, float]:
        """(measurement - prediction, its variance)."""
        return z - self.x[0], self.p[0][0] + r

    def correct(self, y: float, s: float) -> None:
        (p00, p01), (_, p11) = self.p
        k0, k1 = p00 / s, p01 / s
        self.x = [self.x[0] + k0 * y, self.x[1] + k1 * y]
        self.p = [[(1 - k0) * p00, (1 - k0) * p01], [(1 - k0) * p01, p11 - k1 * p01]]


class TargetEstimator:
    """Two-axis spot estimator; feed :meth:`update` once per frame.

    Args:
        accel_noise: How hard the spot's angular velocity may change, as the spectral
            density of its acceleration in deg^2/s^3. Higher follows sudden changes
            sooner but passes more noise.
        max_rate_deg_s: Velocity estimates are clamped to this.
        gate_sigma: A frame whose surprise exceeds this many standard deviations
            (combined over both axes) is treated as a possible glitch.
    """

    def __init__(
        self,
        *,
        accel_noise: float = 1000.0,
        max_rate_deg_s: float = 180.0,
        gate_sigma: float = 4.0,
    ) -> None:
        """Create an empty estimator."""
        self.accel_noise = accel_noise
        self.max_rate_deg_s = max_rate_deg_s
        self.gate_sigma = gate_sigma
        self._axes = (_Axis(), _Axis())
        self.reset()

    def reset(self) -> None:
        """Forget the spot (call when a lock starts or tracking is lost)."""
        self._t: float | None = None
        # The last frame that looked like a glitch: (time, measurement).
        self._suspect: tuple[float, tuple[float, float]] | None = None
        self.rejected = 0
        self.restarts = 0

    @property
    def started(self) -> bool:
        """Whether the first measurement has arrived."""
        return self._t is not None

    def update(
        self, t: float, z: tuple[float, float], sigma_deg: float
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        """Add the spot's direction ``z`` measured at capture time ``t``.

        ``sigma_deg`` is the measurement's standard deviation. Returns the estimated
        (position at ``t``, velocity) in degrees and deg/s.
        """
        r = max(sigma_deg, 1e-4) ** 2
        if self._t is None:
            for axis, value in zip(self._axes, z, strict=True):
                axis.start(value, r, 0.0, 30.0**2)
            self._t = t
            return self._state()
        dt = t - self._t
        if dt < 0:  # an older frame than one already used (the delay estimate moved)
            return self._state()
        self._t = t
        for axis in self._axes:
            axis.predict(dt, self.accel_noise)
        innovations = [axis.innovation(value, r) for axis, value in zip(self._axes, z, strict=True)]
        surprise = sum(y * y / s for y, s in innovations)
        if surprise > self.gate_sigma**2:
            suspect = self._suspect
            if suspect is not None and t > suspect[0]:
                # Two surprises in a row: the motion changed. Restart from those two frames.
                # (Requiring them to agree in direction would let noise on the quiet axis
                # keep rejecting every frame of a real change.)
                span = t - suspect[0]
                for i, axis in enumerate(self._axes):
                    rate = (z[i] - suspect[1][i]) / span
                    rate = max(-self.max_rate_deg_s, min(self.max_rate_deg_s, rate))
                    axis.start(z[i], r, rate, 2 * r / (span * span) + 10.0**2)
                self._suspect = None
                self.restarts += 1
                return self._state()
            self._suspect = (t, z)
            self.rejected += 1
            return self._state()  # the prediction stands until the next frame decides
        self._suspect = None
        for axis, (y, s) in zip(self._axes, innovations, strict=True):
            axis.correct(y, s)
        for axis in self._axes:
            axis.x[1] = max(-self.max_rate_deg_s, min(self.max_rate_deg_s, axis.x[1]))
        return self._state()

    def _state(self) -> tuple[tuple[float, float], tuple[float, float]]:
        yaw, pitch = self._axes
        return (yaw.x[0], pitch.x[0]), (yaw.x[1], pitch.x[1])

    @staticmethod
    def pixel_sigma_deg(pixels: float, width: int, hfov_deg: float, zoom: float = 1.0) -> float:
        """Angle that ``pixels`` of tracker noise spans near the image centre."""
        focal = (width / 2) / (math.tan(math.radians(hfov_deg) / 2) / zoom)
        return math.degrees(pixels / focal)
