"""Tests for the dashboard's point-and-drag gimbal control."""

from __future__ import annotations

import math
import time

import pytest

from web_ui.pointing import AttitudeHistory, center_for, clamp_attitude, screen_to_world

OPTICS = {"aspect": 16 / 9, "hfov_deg": 80.0}


def test_image_edge_is_half_the_field_of_view() -> None:
    yaw, pitch = screen_to_world(0.0, 0.0, 0.5, 0.0, zoom=1.0, **OPTICS)
    assert yaw == pytest.approx(40.0)
    assert pitch == pytest.approx(0.0)
    # Top edge: vertical FOV follows from the aspect ratio.
    vfov = 2 * math.degrees(math.atan(math.tan(math.radians(40)) * 9 / 16))
    assert screen_to_world(0.0, 0.0, 0.0, -0.5, zoom=1.0, **OPTICS)[1] == pytest.approx(vfov / 2)


def test_zoom_narrows_the_field_of_view() -> None:
    yaw, _ = screen_to_world(10.0, 0.0, 0.5, 0.0, zoom=2.0, **OPTICS)
    assert yaw == pytest.approx(10.0 + math.degrees(math.atan(math.tan(math.radians(40)) / 2)))


@pytest.mark.parametrize("pitch", [-80.0, -45.0, 0.0, 20.0])
@pytest.mark.parametrize(("x", "y"), [(0.3, -0.2), (-0.45, 0.4), (0.1, 0.1)])
def test_center_for_inverts_screen_to_world(pitch: float, x: float, y: float) -> None:
    world = screen_to_world(15.0, pitch, x, y, zoom=1.5, **OPTICS)
    center = center_for(*world, x, y, zoom=1.5, pitch_hint=pitch + 3, **OPTICS)
    assert center == pytest.approx((15.0, pitch), abs=0.01)
    # Without a hint the solver may pick the other valid pose near nadir; it must still be valid.
    center = center_for(*world, x, y, zoom=1.5, **OPTICS)
    assert screen_to_world(*center, x, y, zoom=1.5, **OPTICS) == pytest.approx(world, abs=0.01)


def test_click_centers_the_clicked_point() -> None:
    world = screen_to_world(5.0, -30.0, 0.25, 0.25, zoom=1.0, **OPTICS)
    yaw, pitch = center_for(*world, 0.0, 0.0, zoom=1.0, **OPTICS)
    assert (yaw, pitch) == pytest.approx(world)
    assert yaw > 5.0 and pitch < -30.0  # right of and below the old center


def test_clamp_to_a8_mini_travel() -> None:
    assert clamp_attitude(200.0, 40.0) == (135.0, 25.0)
    assert clamp_attitude(-200.0, -100.0) == (-135.0, -90.0)


def test_history_interpolates_and_clamps() -> None:
    history = AttitudeHistory()
    assert history.at(1.0) is None
    history.add(1.0, 0.0, 0.0)
    history.add(2.0, 10.0, -20.0)
    assert history.at(1.5) == pytest.approx((5.0, -10.0))
    assert history.at(0.0) == (0.0, 0.0)
    assert history.at(9.0) == (10.0, -20.0)


class _FakeClient:
    def __init__(self) -> None:
        self.targets: list[tuple[float, float]] = []
        self.zooms: list[float] = []

    async def set_attitude_nowait(self, yaw: float, pitch: float) -> None:
        self.targets.append((yaw, pitch))

    async def absolute_zoom(self, zoom: float) -> None:
        self.zooms.append(zoom)


@pytest.fixture
def camera():
    pytest.importorskip("fastapi")
    pytest.importorskip("cv2")
    from web_ui import server

    state = server.CameraState()
    state.client = _FakeClient()
    state.is_connected = True
    state.zoom, state.zoom_max = 1.0, 6.0
    state.pointing.video_delay_ms = 0
    state.attitude_history.add(time.monotonic(), 10.0, -20.0)
    return state, server.LookRequest


async def test_look_click_targets_the_clicked_point(camera) -> None:
    state, look_request = camera
    hfov = state.pointing.hfov_deg
    result = await state.look(look_request(anchor_x=0.5, anchor_y=0.0))
    [(yaw, pitch)] = state.client.targets
    expected = screen_to_world(10.0, -20.0, 0.5, 0.0, aspect=16 / 9, zoom=1.0, hfov_deg=hfov)
    assert (yaw, pitch) == pytest.approx(expected)
    assert result["limited"] is False


async def test_look_drag_keeps_the_gesture_start_pose(camera) -> None:
    state, look_request = camera
    await state.look(look_request(anchor_x=0.0, anchor_y=0.0, to_x=0.1, to_y=0.0, gesture="g1"))
    # A newer attitude sample must not move the drag's reference.
    state.attitude_history.add(time.monotonic() + 1, 50.0, 0.0)
    await state.look(look_request(anchor_x=0.0, anchor_y=0.0, to_x=0.2, to_y=0.0, gesture="g1"))
    first, second = state.client.targets
    # Dragging the scene right turns the camera left, further for a longer drag.
    assert second[0] < first[0] < 10.0


async def test_look_zoom_sets_zoom_and_honours_limit(camera) -> None:
    state, look_request = camera
    await state.look(look_request(anchor_x=0.0, anchor_y=0.0, zoom=9.0))
    assert state.client.zooms == [6.0]
    assert state.zoom == 6.0


async def test_look_inverted_yaw(camera) -> None:
    state, look_request = camera
    state.pointing.yaw_sign = -1
    await state.look(look_request(anchor_x=0.25, anchor_y=0.0))
    [(yaw, _)] = state.client.targets
    assert yaw < 10.0


async def test_look_reports_travel_limit(camera) -> None:
    state, look_request = camera
    state.attitude_history.add(time.monotonic() + 0.01, 130.0, 0.0)
    state.pointing.video_delay_ms = -1000  # read the newest sample
    result = await state.look(look_request(anchor_x=0.5, anchor_y=0.0))
    assert state.client.targets[0][0] == 135.0
    assert result["limited"] is True
