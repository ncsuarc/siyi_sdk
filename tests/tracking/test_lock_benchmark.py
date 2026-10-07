"""Benchmark the rate-mode point lock on the A8 Mini's measured timing (runs in real time).

Each profile locks a spot and scores the *true* pointing error with LockMetrics: RMS while
tracking, time for a jump's error to halve and to settle, overshoot, and how often the speed
command reverses (jitter). Run with ``-s`` to see the numbers. The asserts are loose
regression guards; the printed numbers are what to compare between controller changes.

Results, two runs each (x64 Python emulated on a Snapdragon X laptop):

=============  =========================================================================
stage          numbers
=============  =========================================================================
baseline       jump: overshoot 5.0-5.1 deg, settle 0.80-0.82 s, half 0.33-0.35 s
(line-fit      swing: rms_all 6.0-7.0 deg, reversals 2.8-3.2 /s
velocity,      drift: rms 0.14-0.15 deg, rms_all 1.04-1.07 deg
lumped delay)  drift_stalls: rms 0.17-0.27, settle 2.13 s, reversals 1.3-2.4 /s (yaw)
=============  =========================================================================
"""

from __future__ import annotations

import asyncio
import math
import time

import pytest

pytest.importorskip("cv2")

from siyi_sdk.tracking import GimbalPointLock, LockMetrics, LoopModel
from tests.tracking.sim import PX_PER_DEG, H, SimConfig, SimGimbal, W, hardware_config, make_ground


def jump(t: float) -> tuple[float, float]:
    """Still, then a fast 6 degree move (40 deg/s) at 2.5 s, then still again."""
    return (min(max(t - 2.5, 0.0), 0.15) * 40.0, 0.0)


def swing(t: float) -> tuple[float, float]:
    """The rig swung side to side: 12 degrees each way at 0.4 Hz (peak 30 deg/s)."""
    return (12.0 * math.sin(2 * math.pi * 0.4 * t), 0.0)


def drift(t: float) -> tuple[float, float]:
    return (8.0 * t, -3.0 * t)


PROFILES = {
    "jump": {"motion": jump},
    "swing": {"motion": swing},
    "drift": {"motion": drift},
    "drift_stalls": {"motion": drift, "frame_jitter": 0.06, "stall_every": 1.5, "stall_s": 0.3},
}


@pytest.fixture(scope="module")
def ground():
    return make_ground()


def model_of(cfg: SimConfig, *, error: float = 1.0) -> LoopModel:
    """The model calibration measures; ``error`` scales its delays to test robustness."""
    delay = (cfg.command_delay + cfg.motor_tau) * error
    return LoopModel(
        (cfg.deg_per_unit, cfg.deg_per_unit), delay, cfg.video_delay, cfg.motor_tau * error
    )


async def run_profile(
    ground, name: str, seconds: float = 6.0, configure=None, *, smith: bool = True,
    model_error: float = 1.0, **overrides,
) -> dict:
    """Run one profile; ``configure(lock)`` may adjust the lock first (for tuning)."""
    cfg = hardware_config(**{**PROFILES[name], **overrides})
    async with SimGimbal(ground, cfg) as sim:
        lock = GimbalPointLock(
            send=sim.rotate, send_angle=sim.set_angle, hfov_deg=80,
            loop=model_of(cfg, error=model_error), attitude=sim.history, control="rate",
            trust_video_delay=True, smith=smith,
        )
        if configure is not None:
            configure(lock)
        first: dict[str, float] = {}
        offset = (3.0, 0.0)

        async def on_frame(frame) -> None:
            if not first:
                first["captured"] = frame.captured
                lock.lock(frame.frame, W / 2 + offset[0] * PX_PER_DEG, H / 2 - offset[1] * PX_PER_DEG)
                return
            await lock.update(frame.frame, timestamp=frame.timestamp)

        sim.frame_callbacks.append(on_frame)
        metrics = LockMetrics()
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            await asyncio.sleep(0.02)
            if not first:
                continue
            t = time.monotonic() - sim.t0
            # The spot as it was at lock time, carried along by the scene's motion since.
            then, now = sim.drift(first["captured"]), sim.drift(t)
            yaw = offset[0] + now[0] - then[0] - sim.yaw
            pitch = offset[1] + now[1] - then[1] - sim.pitch
            command = sim.sent[-1][1:] if sim.sent else (0, 0)
            metrics.add(time.monotonic(), (yaw, pitch), command)
        await lock.release()
    return metrics.summary()


@pytest.mark.parametrize("name", list(PROFILES))
async def test_rate_lock_benchmark(ground, name):
    score = await run_profile(ground, name)
    print(f"\n{name}: {score}")
    assert score["max_gap_ms"] < 1000
    if name == "jump":
        assert score["events"] >= 1 and score["unsettled_events"] == 0
    if name in ("drift", "jump"):
        assert score["rms_deg"] is not None and score["rms_deg"] < 1.0


@pytest.mark.parametrize("model_error", [0.7, 1.3])
async def test_predictor_tolerates_a_wrong_model(ground, model_error):
    """Delays measured 30% short or long must not make the predictor oscillate."""
    score = await run_profile(ground, "jump", model_error=model_error)
    print(f"\njump with model delays x{model_error}: {score}")
    assert score["unsettled_events"] == 0
    assert score["rms_all_deg"] < 3.0
