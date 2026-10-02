"""Tests for the OpenCV-free rate controller."""

from __future__ import annotations

import math

import pytest

from siyi_sdk.tracking.control import LockGains, RateController, pixel_error_deg


def test_pixel_error_signs_and_scale() -> None:
    assert pixel_error_deg(640, 360, 1280, 720, hfov_deg=80) == pytest.approx((0.0, 0.0))
    yaw, pitch = pixel_error_deg(1280, 0, 1280, 720, hfov_deg=80)
    assert yaw == pytest.approx(40.0)  # right edge = half the field of view
    vertical_half = math.degrees(math.atan(math.tan(math.radians(40)) * 720 / 1280))
    assert pitch == pytest.approx(vertical_half)  # top edge, positive = up
    zoomed, _ = pixel_error_deg(1280, 360, 1280, 720, hfov_deg=80, zoom=2)
    assert zoomed == pytest.approx(math.degrees(math.atan(math.tan(math.radians(40)) / 2)))


def test_pixel_error_off_screen_keeps_growing() -> None:
    inside, _ = pixel_error_deg(1280, 360, 1280, 720, hfov_deg=80)
    outside, _ = pixel_error_deg(2560, 360, 1280, 720, hfov_deg=80)
    assert outside > inside


def test_controller_signs_deadband_and_saturation() -> None:
    controller = RateController(LockGains(kp=2, ki=0, max_step=1000))
    assert controller.update(0.1, -0.1, 0.03) == (0, 0)  # inside deadband
    assert controller.update(5, -5, 0.03) == (10, -10)
    assert controller.update(500, -500, 0.03) == (60, -60)  # max_speed


def test_controller_slew_limit() -> None:
    controller = RateController(LockGains(kp=10, ki=0, max_step=20))
    speeds = [controller.update(10, 0, 0.03)[0] for _ in range(4)]
    assert speeds == [20, 40, 60, 60]


def test_integral_removes_steady_error_and_is_bounded() -> None:
    controller = RateController(LockGains(kp=0, ki=1, max_speed=60, max_step=1000))
    for _ in range(100):
        yaw, _ = controller.update(2, 0, 0.1)
    assert yaw == 20  # 2 deg for 10 s
    for _ in range(1000):
        yaw, _ = controller.update(50, 0, 0.5)
    assert yaw == 60
    controller.reset()
    assert controller.update(0, 0, 0.1) == (0, 0)


def test_long_gaps_do_not_wind_up() -> None:
    controller = RateController(LockGains(kp=0, ki=1, max_step=1000))
    yaw, _ = controller.update(2, 0, 30.0)  # a 30 s stall counts as 0.5 s
    assert yaw == 1  # 2 deg x 0.5 s, not 2 deg x 30 s
