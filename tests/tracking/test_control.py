"""Tests for the OpenCV-free rate controller and attitude history."""

from __future__ import annotations

import math

import pytest

from siyi_sdk.tracking import AttitudeHistory
from siyi_sdk.tracking.control import LockGains, LoopModel, RateController, pixel_error_deg


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


def test_attitude_history_interpolates_and_reports_latest() -> None:
    history = AttitudeHistory()
    assert history.at(1.0) is None and history.latest() is None
    history.add(1.0, 0.0, 0.0)
    history.add(2.0, 10.0, -20.0)
    assert history.at(1.5) == pytest.approx((5.0, -10.0))
    assert history.at(0.0) == (0.0, 0.0)
    assert history.latest() == (2.0, 10.0, -20.0)
