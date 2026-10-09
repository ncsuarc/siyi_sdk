"""Regression checks for scheduler, numerical kernels and exact long-lock metrics."""

from __future__ import annotations

import math
import random
from collections import deque

import numpy as np
import pytest

from siyi_sdk.tracking.attitude import AttitudeHistory
from siyi_sdk.tracking.calibrate import _video_delay
from siyi_sdk.tracking.control import GimbalPredictor
from siyi_sdk.tracking.gimbal import GimbalPointLock
from siyi_sdk.tracking.metrics import LockMetrics


def original_turn(model, start, end):
    if not model._sent:
        return 0.0, 0.0
    tau, step = model.motor_tau_s, model.STEP_S
    t = start - 5 * tau - step
    speed = list(model._commanded(t))
    turned = [0.0, 0.0]
    while t < end:
        target = model._commanded(t)
        blend = 1.0 if tau <= 0 else min(1.0, step / tau)
        for axis in (0, 1):
            speed[axis] += (target[axis] - speed[axis]) * blend
            if t >= start:
                turned[axis] += speed[axis] * step
        t += step
    return tuple(turned)


def test_predictor_preserves_irregular_discrete_integration():
    rng = random.Random(47)
    for _ in range(100):
        model = GimbalPredictor(rng.choice([0, 0.04, 0.12]), rng.choice([0, 0.07, 0.15]))
        for t in sorted(rng.random() * 2 for _ in range(60)):
            model.record(t, (rng.uniform(-20, 20), rng.uniform(-20, 20)))
        start = rng.uniform(1, 2)
        end = start + rng.uniform(0, 0.2)
        assert model.turned(start, end) == original_turn(model, start, end)


@pytest.mark.parametrize("offset", [0, 1e6])
@pytest.mark.parametrize("constant", [False, True])
def test_calibration_fit_preserves_lag_scale_and_degenerate_input(offset, constant):
    att_t = np.linspace(0, 4, 200) + offset
    img_t = np.linspace(0.8, 3.5, 100) + offset
    att_v = np.ones(200) if constant else np.sin((att_t - offset) * 4)
    img_v = 1.2 * np.interp(img_t - 0.2, att_t, att_v) + 0.3
    expected = (0, 1, math.inf)
    for lag in np.arange(0, 0.8, 0.005):
        att = np.interp(img_t - lag, att_t, att_v)
        a = np.vstack([att, np.ones_like(att)]).T
        (k, b), *_ = np.linalg.lstsq(a, img_v, rcond=None)
        rms = float(np.sqrt(np.mean((a @ np.array([k, b]) - img_v) ** 2)))
        if rms < expected[2]:
            expected = float(lag), float(k), rms
    assert _video_delay(att_t, att_v, img_t, img_v) == pytest.approx(expected, abs=1e-10)


async def no_send(*args):
    pass


@pytest.mark.parametrize("hz", [0, -1, math.nan, math.inf])
def test_control_frequency_validation(hz):
    with pytest.raises(ValueError, match="control_hz"):
        GimbalPointLock(send=no_send, control_hz=hz)


async def test_scheduler_shares_period_with_work_and_skips_overruns(monkeypatch):
    import siyi_sdk.tracking.gimbal as module

    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    sleeps, steps = [], []

    async def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    lock = GimbalPointLock(send=no_send, control_hz=50)
    lock._tracker = object()

    async def step(now, dt):
        steps.append((now, dt))
        clock[0] += 0.055 if len(steps) == 2 else 0.005
        if len(steps) == 4:
            lock._tracker = None

    lock._control_step = step
    await lock._control_loop()
    assert [s[0] for s in steps] == pytest.approx([0.02, 0.04, 0.1, 0.12])
    assert sleeps == pytest.approx([0.02, 0.015, 0.005, 0.015])


async def test_exact_long_lock_metrics():
    metrics = LockMetrics()
    values = [0.1 + (i % 173) / 200 for i in range(180000)]
    for i, value in enumerate(values):
        metrics.add(i / 50, (value, 0), (0, 0))
    summary = await metrics.summary_async()
    expected = sorted(values)
    assert summary["rms_deg"] == round(math.sqrt(sum(v * v for v in values) / len(values)), 3)
    assert summary["p95_deg"] == round(expected[int(len(values) * 0.95)], 3)
    assert summary == metrics.summary()


def test_online_delay_fit_preserves_result_with_large_clock():
    for offset in (0.0, 1e6):
        history = AttitudeHistory(seconds=10)
        for t in np.linspace(0, 4, 201):
            history.add(offset + t, 8 * math.sin(t * 4), 3 * math.cos(t * 3))
        lock = GimbalPointLock(send=no_send, attitude=history)
        lock.loop.video_delay_s = 0.18
        arrivals = np.linspace(1, 3, 50) + offset
        samples = np.array(history.samples)
        lock._frames = deque(
            (
                t,
                -np.interp(t - 0.2, samples[:, 0], samples[:, 1]),
                -np.interp(t - 0.2, samples[:, 0], samples[:, 2]),
            )
            for t in arrivals
        )
        lock._refine_video_delay()
        assert lock.loop.video_delay_s == pytest.approx(0.188, abs=1e-8)


@pytest.mark.parametrize(
    "profile", ["pan", "occlusion", "low_texture", "foreground", "loss", "skips"]
)
def test_decoder_grayscale_quality_gate(profile):
    pytest.importorskip("av")
    from scripts.performance_workloads import scene_frames, tracking_trial
    from siyi_sdk.tracking.point_lock import PointLock

    frames, truths = scene_frames()
    bgr = tracking_trial(PointLock, frames, truths, profile=profile)
    gray = tracking_trial(PointLock, frames, truths, fmt="gray", profile=profile)
    assert gray["states"] == bgr["states"]
    assert gray["rms_px"] <= bgr["rms_px"] + 0.25
    assert gray["max_px"] <= bgr["max_px"] + 1.0


async def test_release_while_sleeping_prevents_control_step(monkeypatch):
    import siyi_sdk.tracking.gimbal as module

    lock = GimbalPointLock(send=no_send)
    lock._tracker = object()

    async def sleep(delay):
        await lock.release()

    async def step(now, dt):
        raise AssertionError("must not steer after release")

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    lock._control_step = step
    await lock._control_loop()


def test_large_private_payload_is_owned_and_corruption_recovers():
    from siyi_sdk.firmware_tracking import FirmwareFrameParser
    from tests.tracking.test_firmware_over_tcp import video_packet

    parser = FirmwareFrameParser()
    damaged = bytearray(video_packet(1, 300000))
    damaged[-1] ^= 1
    valid = video_packet(2, 300000)
    assert parser.feed(bytes(damaged[:100000])) == []
    frames = parser.feed(bytes(damaged[100000:]) + valid)
    assert len(frames) == 1
    retained = frames[0].payload
    assert retained[6:] == bytes([2]) * 300000
    parser.feed(b"junk" * 100000)
    assert frames[0].payload == retained
