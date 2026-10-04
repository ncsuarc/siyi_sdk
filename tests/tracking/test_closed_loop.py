"""Closed-loop point lock and calibration against a simulated gimbal (runs in real time)."""

from __future__ import annotations

import asyncio
import statistics
import time

import pytest

pytest.importorskip("cv2")

from siyi_sdk.tracking import (
    FrameMotionRecorder,
    GimbalPointLock,
    LoopModel,
    calibrate_loop,
)
from tests.tracking.sim import PX_PER_DEG, H, SimConfig, SimGimbal, W, make_ground


@pytest.fixture(scope="module")
def ground():
    return make_ground()


async def hold(sim: SimGimbal, lock: GimbalPointLock, seconds: float, offset=(10.0, 5.0)):
    """Lock a spot ``offset`` degrees from centre; return [(t, true error deg)]."""
    first: dict[str, float] = {}

    async def on_frame(frame) -> None:
        if not first:
            first["captured"] = frame.captured
            lock.lock(frame.frame, W / 2 + offset[0] * PX_PER_DEG, H / 2 - offset[1] * PX_PER_DEG)
            return
        await lock.update(frame.frame, timestamp=frame.timestamp)

    sim.frame_callbacks.append(on_frame)
    trace = []
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        await asyncio.sleep(0.02)
        if first:
            dx, dy = sim.drift(first["captured"])
            spot = (offset[0] - dx, offset[1] - dy)
            trace.append((time.monotonic() - start, sim.error_to(spot, time.monotonic() - sim.t0)))
    await lock.release()
    return trace


def model_of(cfg: SimConfig) -> LoopModel:
    # Dead time plus motor lag, as calibration measures it.
    delay = cfg.command_delay + cfg.motor_tau
    return LoopModel((cfg.deg_per_unit, cfg.deg_per_unit), delay, cfg.video_delay)


@pytest.mark.parametrize("control", ["rate", "angle"])
async def test_compensated_lock_settles_quickly_and_follows_a_moving_drone(ground, control):
    cfg = SimConfig(drift_deg_s=(8.0, -3.0), attitude_signs=(-1, 1))
    async with SimGimbal(ground, cfg) as sim:
        lock = GimbalPointLock(
            send=sim.rotate,
            send_angle=sim.set_angle,
            hfov_deg=80,
            loop=model_of(cfg),
            attitude=sim.history,
            attitude_signs=cfg.attitude_signs,
            control=control,
            trust_video_delay=True,  # the model is exact, as after calibration
        )
        trace = await hold(sim, lock, 3.0)
    # 11 degrees off at the start; within 1 degree after 1.2 s and staying there.
    assert max(e for t, e in trace if t > 1.2) < 1.0
    assert statistics.median(e for t, e in trace if t > 2.0) < 0.5


@pytest.mark.parametrize("control", ["rate", "angle"])
@pytest.mark.parametrize("assumed_delay", [0.14, 0.26])
async def test_wrong_video_delay_is_corrected_while_locked(ground, control, assumed_delay):
    # Truth is 0.20 s. Before online estimation, +60 ms made both modes run away.
    cfg = SimConfig(drift_deg_s=(8.0, -3.0))
    async with SimGimbal(ground, cfg) as sim:
        loop = LoopModel((0.9, 0.9), 0.11, assumed_delay)  # turn rate 25% off too
        lock = GimbalPointLock(
            send=sim.rotate,
            send_angle=sim.set_angle,
            hfov_deg=80,
            loop=loop,
            attitude=sim.history,
            control=control,
        )
        trace = await hold(sim, lock, 4.0)
    assert loop.video_delay_s == pytest.approx(0.20, abs=0.02)
    assert max(e for t, e in trace if t > 2.5) < 1.0
    assert statistics.median(e for t, e in trace if t > 3.0) < 0.6


async def test_uncompensated_fallback_still_converges(ground):
    cfg = SimConfig()
    async with SimGimbal(ground, cfg) as sim:
        lock = GimbalPointLock(send=sim.rotate, hfov_deg=80, loop=model_of(cfg))
        trace = await hold(sim, lock, 4.0)
    assert statistics.median(e for t, e in trace if t > 3.0) < 1.0


async def test_calibration_recovers_the_simulated_gimbal(ground):
    cfg = SimConfig(deg_per_unit=0.8, video_delay=0.25, attitude_signs=(-1, -1))
    async with SimGimbal(ground, cfg) as sim:
        recorder = FrameMotionRecorder()
        sim.frame_callbacks.append(lambda f: recorder.add(f.frame, f.timestamp))
        result = await calibrate_loop(
            sim.rotate, sim.history, recorder, hfov_deg=74.0, move_s=0.6, speed=30
        )
    model = result.model
    assert model.deg_per_unit == pytest.approx((0.8, 0.8), rel=0.05)
    assert model.video_delay_s == pytest.approx(0.25, abs=0.03)
    assert 0.05 < model.command_delay_s < 0.15  # 50 ms dead time + 60 ms motor lag
    assert result.attitude_signs == (-1, -1)
    assert result.hfov_deg == pytest.approx(80.0, abs=2.5)  # corrected from a wrong 74
    assert not result.notes
