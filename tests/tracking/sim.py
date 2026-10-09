"""Real-time simulated gimbal, camera and aircraft for closed-loop point-lock tests.

The gimbal obeys 0x07 speed commands (after a command delay, with a first-order
motor lag) and 0x0E angle targets (its own position loop). It publishes
attitude at ``attitude_hz`` into an AttitudeHistory. The camera renders a
textured ground at ``fps`` and delivers each frame ``video_delay`` seconds late.
The aircraft drifts so a fixed ground spot moves across the sky at
``drift_deg_s``.

Angles: "image" yaw is to the right and pitch is up; the reported attitude is
``attitude_signs`` times that, as on a camera whose conventions are unknown.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import cv2
import numpy as np

from siyi_sdk.tracking.attitude import AttitudeHistory

W, H = 640, 360
HFOV = 80.0
PX_PER_DEG = (W / 2) / math.tan(math.radians(HFOV / 2)) * math.pi / 180


def make_ground(seed: int = 11) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = (rng.random((2400, 4000, 3)) * 255).astype(np.uint8)
    return cv2.GaussianBlur(noise, (0, 0), 2.5)


@dataclass
class SimConfig:
    deg_per_unit: float = 1.2  # image-aligned deg/s per 0x07 unit
    command_delay: float = 0.05
    motor_tau: float = 0.06
    video_delay: float = 0.20
    fps: float = 30.0
    attitude_hz: float = 50.0
    attitude_signs: tuple[int, int] = (1, 1)
    drift_deg_s: tuple[float, float] = (0.0, 0.0)
    angle_bandwidth: float = 12.0  # 1/s, the gimbal's own position loop
    max_rate: float = 150.0
    # 0x07 units the motor ignores; above it the rate is deg_per_unit * (units - deadzone).
    deadzone_units: float = 0.0
    # Where a ground spot has moved across the sky by time t (image-aligned degrees);
    # overrides drift_deg_s. Models sudden target motion and the rig being swung.
    motion: Callable[[float], tuple[float, float]] | None = None
    # Extra random delay of each frame's delivery (0..frame_jitter s); frames stay in
    # order, so a late frame holds back the next ones, as the camera's bursts do.
    frame_jitter: float = 0.0
    # Freeze the whole event loop for stall_s every stall_every seconds (0 = never), as
    # a CPU-starved server does. The gimbal keeps turning at the last speed meanwhile.
    stall_every: float = 0.0
    stall_s: float = 0.3
    seed: int = 1


def hardware_config(**overrides: object) -> SimConfig:
    """Timing measured on the A8 Mini over the firmware video link (October 2026)."""
    values: dict[str, object] = {
        "deg_per_unit": 0.68,
        "command_delay": 0.10,  # dead time; with motor_tau, 130 ms as calibration measures
        "motor_tau": 0.03,
        "video_delay": 0.04,
        "fps": 25.0,
    }
    values.update(overrides)
    return SimConfig(**values)  # type: ignore[arg-type]


@dataclass
class SimFrame:
    frame: np.ndarray
    timestamp: float
    captured: float = 0.0  # seconds since the simulation started
    width: int = W
    height: int = H


@dataclass
class SimGimbal:
    ground: np.ndarray
    cfg: SimConfig = field(default_factory=SimConfig)

    def __post_init__(self) -> None:
        self.yaw = 0.0  # image-aligned degrees
        self.pitch = 0.0
        self.rate = [0.0, 0.0]
        self.mode = "rate"
        self.angle_target = (0.0, 0.0)
        self.pending: deque[tuple[float, str, tuple[float, float]]] = deque()
        self.command_target = (0.0, 0.0)
        self.history = AttitudeHistory()
        self.frame_callbacks: list[Callable[[SimFrame], Awaitable[None] | None]] = []
        self.t0 = time.monotonic()
        self.sent: list[tuple[float, int, int]] = []
        self._tasks: list[asyncio.Task[None]] = []

    # -- commands ---------------------------------------------------------------
    async def rotate(self, yaw: int, pitch: int) -> None:
        self.sent.append((time.monotonic(), yaw, pitch))
        self.pending.append((time.monotonic() + self.cfg.command_delay, "rate", (yaw, pitch)))

    async def set_angle(self, yaw: float, pitch: float) -> None:
        sy, sp = self.cfg.attitude_signs
        self.pending.append(
            (time.monotonic() + self.cfg.command_delay, "angle", (yaw * sy, pitch * sp))
        )

    # -- world ------------------------------------------------------------------
    def drift(self, t: float) -> tuple[float, float]:
        if self.cfg.motion is not None:
            return self.cfg.motion(t)
        return self.cfg.drift_deg_s[0] * t, self.cfg.drift_deg_s[1] * t

    def render(self, t: float) -> np.ndarray:
        dx, dy = self.drift(t)
        cx = 2000 + (self.yaw - dx) * PX_PER_DEG
        cy = 1200 - (self.pitch - dy) * PX_PER_DEG
        m = np.float32([[1, 0, W / 2 - cx], [0, 1, H / 2 - cy]])
        return cv2.warpAffine(self.ground, m, (W, H), flags=cv2.INTER_LINEAR)

    def error_to(self, spot: tuple[float, float], t: float) -> float:
        """True angle from the camera centre to a ground spot given in image-aligned degrees."""
        dx, dy = self.drift(t)
        return math.hypot(spot[0] + dx - self.yaw, spot[1] + dy - self.pitch)

    # -- loops ------------------------------------------------------------------
    async def _physics(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.002)
            now = time.monotonic()
            dt, last = now - last, now
            while self.pending and self.pending[0][0] <= now:
                _, mode, value = self.pending.popleft()
                self.mode = mode
                if mode == "rate":
                    dz = self.cfg.deadzone_units
                    self.command_target = tuple(  # type: ignore[assignment]
                        math.copysign(max(abs(u) - dz, 0.0), u) * self.cfg.deg_per_unit
                        for u in value
                    )
                else:
                    self.angle_target = value
            for axis in range(2):
                if self.mode == "rate":
                    wanted = self.command_target[axis]
                else:
                    position = self.yaw if axis == 0 else self.pitch
                    wanted = self.cfg.angle_bandwidth * (self.angle_target[axis] - position)
                wanted = max(-self.cfg.max_rate, min(self.cfg.max_rate, wanted))
                self.rate[axis] += (wanted - self.rate[axis]) * min(1.0, dt / self.cfg.motor_tau)
            self.yaw += self.rate[0] * dt
            self.pitch += self.rate[1] * dt

    async def _attitude(self) -> None:
        sy, sp = self.cfg.attitude_signs
        while True:
            self.history.add(time.monotonic(), self.yaw * sy, self.pitch * sp)
            await asyncio.sleep(1 / self.cfg.attitude_hz)

    async def _camera(self) -> None:
        queue: deque[tuple[float, float, np.ndarray]] = deque()
        next_frame = time.monotonic()
        rng = np.random.default_rng(self.cfg.seed)
        while True:
            now = time.monotonic()
            if now >= next_frame:
                due = now + self.cfg.video_delay + rng.uniform(0.0, self.cfg.frame_jitter)
                if queue:
                    due = max(due, queue[-1][0])
                queue.append((due, now - self.t0, self.render(now - self.t0)))
                next_frame += 1 / self.cfg.fps
            while queue and queue[0][0] <= now:
                _, captured, image = queue.popleft()
                # Stamped as the firmware link's CaptureClock does: capture time plus the
                # least-held frame's delay, so a frame held up by jitter or a stall still
                # carries its true timing.
                stamp = self.t0 + captured + self.cfg.video_delay
                frame = SimFrame(image, stamp, captured)
                for cb in self.frame_callbacks:
                    result = cb(frame)
                    if asyncio.iscoroutine(result):
                        await result
            await asyncio.sleep(0.003)

    async def _stalls(self) -> None:
        while self.cfg.stall_every > 0:
            await asyncio.sleep(self.cfg.stall_every)
            time.sleep(self.cfg.stall_s)  # blocks every task, like a starved process

    async def __aenter__(self) -> SimGimbal:
        self.t0 = time.monotonic()
        self._tasks = [
            asyncio.create_task(c())
            for c in (self._physics, self._attitude, self._camera, self._stalls)
        ]
        await asyncio.sleep(0.3)  # fill the attitude history and video pipe
        return self

    async def __aexit__(self, *exc: object) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
