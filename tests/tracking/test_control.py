"""Tests for the OpenCV-free rate controller and attitude history."""

from __future__ import annotations

import math

import pytest

from siyi_sdk.tracking import AttitudeHistory
from siyi_sdk.tracking.control import (
    GimbalPredictor,
    LockGains,
    LoopModel,
    RateController,
    OscillationGuard,
    TurnRateEstimator,
    pixel_error_deg,
)


def test_pixel_error_signs_and_scale() -> None:
    assert pixel_error_deg(640, 360, 1280, 720, hfov_deg=80) == pytest.approx((0.0, 0.0))
    yaw, pitch = pixel_error_deg(1280, 0, 1280, 720, hfov_deg=80)
    assert yaw == pytest.approx(40.0)  # right edge = half the field of view
    vertical_half = math.degrees(math.atan(math.tan(math.radians(40)) * 720 / 1280))
    assert pitch == pytest.approx(vertical_half)  # top edge, positive = up
    zoomed, _ = pixel_error_deg(1280, 360, 1280, 720, hfov_deg=80, zoom=2)
    assert zoomed == pytest.approx(math.degrees(math.atan(math.tan(math.radians(40)) / 2)))


def test_gains_follow_the_measured_delay() -> None:
    model = LoopModel(command_delay_s=0.05, video_delay_s=0.25)
    fast = LockGains.for_model(model, compensated=True)
    slow = LockGains.for_model(model, compensated=False)
    assert fast.kp == pytest.approx(1 / (2 * 0.05))
    assert slow.kp == pytest.approx(1 / (2 * 0.30))
    assert fast.kp > 5 * slow.kp


def test_output_is_converted_with_the_measured_turn_rate() -> None:
    gains = LockGains(kp=4, ki=0)
    fast_gimbal = RateController(gains, LoopModel(deg_per_unit=(2.0, 2.0)))
    slow_gimbal = RateController(gains, LoopModel(deg_per_unit=(0.5, 0.5)))
    assert fast_gimbal.update(5, -5, 0.02) == (10, -10)  # 20 deg/s at 2 deg/s per unit
    assert slow_gimbal.update(5, -5, 0.02) == (40, -40)


def test_negative_turn_rate_flips_the_command() -> None:
    controller = RateController(LockGains(kp=4, ki=0), LoopModel(deg_per_unit=(-1.0, 1.0)))
    assert controller.update(5, 5, 0.02) == (-20, 20)


def test_deadband_saturation_and_feedforward() -> None:
    controller = RateController(LockGains(kp=4, ki=0, max_speed=60), LoopModel())
    assert controller.update(0.1, -0.1, 0.02) == (0, 0)  # inside deadband
    assert controller.update(500, -500, 0.02) == (60, -60)  # max_speed
    # Zero error but a moving target: feedforward alone keeps up with it.
    assert controller.update(0, 0, 0.02, feedforward=(8.0, -3.0)) == (8, -3)


def test_integral_only_near_target_and_bounded() -> None:
    controller = RateController(LockGains(kp=0, ki=1, integral_zone_deg=2.0), LoopModel())
    for _ in range(50):
        controller.update(10, 0, 0.1)  # far away: must not wind up
    assert controller.update(10, 0, 0.1) == (0, 0)
    for _ in range(100):
        yaw, _ = controller.update(1.5, 0, 0.1)
    assert yaw == 15  # 1.5 deg for 10 s
    controller.reset()
    assert controller.update(0, 0, 0.1) == (0, 0)


def test_rounding_is_carried_so_the_average_speed_is_exact() -> None:
    # 3.3 deg/s at 1 deg/s per unit: plain rounding would send 3 forever. (Rates above
    # still_rate_deg_s, so the deadband doesn't hold a target with zero error.)
    controller = RateController(LockGains(kp=0, ki=0), LoopModel())
    sent = [controller.update(0, 0, 0.02, feedforward=(3.3, 3.25))[0] for _ in range(100)]
    assert sum(sent) == pytest.approx(330, abs=1)
    controller.reset()
    pitch = [controller.update(0, 0, 0.02, feedforward=(0.0, 3.25))[1] for _ in range(100)]
    assert sum(pitch) == pytest.approx(325, abs=1)
    assert set(pitch) <= {3, 4}


def test_saturated_commands_carry_no_remainder() -> None:
    controller = RateController(LockGains(kp=4, ki=0, max_speed=60), LoopModel())
    controller.update(500, 0, 0.02)
    assert controller.update(0, 0, 0.02, feedforward=(3.0, 0.0)) == (3, 0)


def test_acceleration_limit_ramps_the_command() -> None:
    gains = LockGains(kp=10, ki=0, max_accel_deg_s2=500)
    controller = RateController(gains, LoopModel())
    first = controller.update(10, 0, 0.02)[0]  # wants 100 deg/s, may only reach 10
    assert first == 10
    assert controller.update(10, 0, 0.02)[0] == 20
    assert RateController(LockGains(kp=10, ki=0), LoopModel()).update(10, 0, 0.02)[0] == 100


def test_deadband_has_hysteresis() -> None:
    controller = RateController(LockGains(kp=10, ki=0), LoopModel())  # band 0.2, release 0.4
    assert controller.update(0.3, 0, 0.02)[0] == 3  # outside: corrects
    assert controller.update(0.1, 0, 0.02)[0] == 0  # inside: holds
    assert controller.update(0.3, 0, 0.02)[0] == 0  # still held below the release
    assert controller.update(0.5, 0, 0.02)[0] == 5  # released
    # Held means nothing at all is sent, not even the integral or a noise-level feedforward.
    controller.reset()
    controller.update(0.3, 0, 0.5)  # builds some integral
    assert controller.update(0.1, 0, 0.02, feedforward=(0.5, 0.0))[0] == 0
    # A moving target is never held, however small the error.
    controller.reset()
    assert controller.update(0.05, 0, 0.02, feedforward=(5.0, 0.0))[0] == 6


def test_attitude_history_interpolates_and_reports_latest() -> None:
    history = AttitudeHistory()
    assert history.at(1.0) is None and history.latest() is None
    history.add(1.0, 0.0, 0.0)
    history.add(2.0, 10.0, -20.0)
    assert history.at(1.5) == pytest.approx((5.0, -10.0))
    assert history.at(0.0) == (0.0, 0.0)
    assert history.latest() == (2.0, 10.0, -20.0)


def _drive(estimator, true_scale, rates, *, step=0.02, disturb=0.0):
    """Run commands through a dead-time + lag gimbal turning ``true_scale`` times the model."""
    truth = GimbalPredictor(estimator.model.dead_time_s, estimator.model.motor_tau_s, 1.0)
    angle, history = [0.0, 0.0], [(0.0, 0.0, 0.0)]
    t = 0.0
    for rate in rates:
        estimator.record(t, rate)
        truth.record(t, (rate[0] * true_scale[0], rate[1] * true_scale[1]))
        turned = truth.turned(t - step, t)
        angle = [angle[0] + turned[0] + disturb * step, angle[1] + turned[1]]
        history.append((t, angle[0], angle[1]))
        old = min(history, key=lambda h: abs(h[0] - (t - estimator.window_s)))
        if t >= estimator.window_s:
            estimator.observe(t, (angle[0], angle[1]), (old[1], old[2]))
        t += step


def test_turn_rate_estimate_converges_to_the_real_turn_rate() -> None:
    estimator = TurnRateEstimator(0.1, 0.03)
    # Back-and-forth sweeps, as a lock hunting a moving target sends.
    rates = [(20.0 * math.sin(i / 15), -10.0 * math.cos(i / 20)) for i in range(400)]
    _drive(estimator, (1.4, 0.7), rates)
    assert estimator.scale == pytest.approx([1.4, 0.7], rel=0.05)


def test_turn_rate_estimate_ignores_a_still_gimbal_and_stays_in_limits() -> None:
    still = TurnRateEstimator(0.1, 0.03)
    _drive(still, (1.5, 1.5), [(0.5, 0.0)] * 200)  # 0.1 deg per window: too little to tell
    assert still.scale == [1.0, 1.0]
    assert still.samples == [0, 0]
    wild = TurnRateEstimator(0.1, 0.03, limits=(0.5, 2.0), outlier_ratio=10.0)
    _drive(wild, (2.8, 1.0), [(15.0, 15.0)] * 200)
    assert wild.scale[0] == 2.0


def test_turn_rate_estimate_drops_turns_the_mount_made_by_itself() -> None:
    estimator = TurnRateEstimator(0.1, 0.03)
    # Commanded 5 deg/s while the aircraft yaws the mount 40 deg/s: not the motor's doing.
    _drive(estimator, (1.0, 1.0), [(5.0, 0.0)] * 200, disturb=40.0)
    assert estimator.scale[0] == 1.0


def test_learned_turn_scale_changes_the_units_sent() -> None:
    controller = RateController(LockGains(kp=4, ki=0), LoopModel(deg_per_unit=(1.0, 1.0)))
    assert controller.update(5, 5, 0.02) == (20, 20)
    controller.turn_scale = [2.0, 0.5]  # yaw turns twice as fast as calibrated, pitch half
    controller.reset()
    assert controller.update(5, 5, 0.02) == (10, 40)
    assert controller.delivered_rate((10, 40)) == (20.0, 20.0)
    assert controller.delivered_rate((10, 40), scaled=False) == (10.0, 40.0)


def test_turn_rate_estimator_refines_the_dead_time() -> None:
    # Calibrated 0.10 s; the link is busier now and commands take 0.14 s to act.
    estimator = TurnRateEstimator(0.10, 0.03)
    truth = TurnRateEstimator(0.14, 0.03)
    rates = [(25.0 * math.sin(i / 9), 15.0 * math.cos(i / 13)) for i in range(600)]
    step, angle, history = 0.02, [0.0, 0.0], [(0.0, 0.0, 0.0)]
    for i, rate in enumerate(rates):
        t = i * step
        estimator.record(t, rate)
        truth.model.record(t, rate)
        turned = truth.model.turned(t - step, t)
        angle = [angle[0] + turned[0], angle[1] + turned[1]]
        history.append((t, *angle))
        old = min(history, key=lambda h: abs(h[0] - (t - estimator.window_s)))
        if t >= 0.5:
            estimator.observe(t, (angle[0], angle[1]), (old[1], old[2]))
    assert estimator.dead_time_s == pytest.approx(0.14, abs=0.015)
    assert estimator.scale == pytest.approx([1.0, 1.0], abs=0.05)


def test_dead_time_stays_put_without_motion() -> None:
    estimator = TurnRateEstimator(0.10, 0.03)
    _drive(estimator, (1.0, 1.0), [(0.3, 0.0)] * 300)
    assert estimator.dead_time_s == 0.10


def test_oscillation_guard_backs_off_on_ringing_and_recovers() -> None:
    guard = OscillationGuard()
    # 1.5 Hz ringing of +-1 deg: three flips per second.
    for i in range(100):
        guard.update((math.sin(2 * math.pi * 1.5 * i * 0.02), 0.0), 0.02)
    assert guard.scale <= 0.7
    assert guard.backoffs >= 1
    cut = guard.scale
    for _ in range(400):  # 8 s calm
        guard.update((0.0, 0.0), 0.02)
    assert cut < guard.scale <= 1.0


def test_oscillation_guard_ignores_small_wobble_and_one_overshoot() -> None:
    guard = OscillationGuard()
    for i in range(500):  # deadband pulsing: +-0.3 deg
        guard.update((0.3 * math.sin(i), 0.0), 0.02)
    for error in [10.0] * 20 + [-0.8] * 20 + [0.0] * 50:  # a step and one overshoot
        guard.update((error, 0.0), 0.02)
    assert guard.scale == 1.0


def test_guard_scales_the_controller_gains() -> None:
    controller = RateController(LockGains(kp=4, ki=0), LoopModel())
    controller.guard.scale = 0.5
    controller.guard._last_backoff = 0.0  # just cut: no recovery this step
    assert controller.update(5, 0, 0.02)[0] == 10
    assert controller.gain_scale == 0.5
