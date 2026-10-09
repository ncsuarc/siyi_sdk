"""Tests for the dashboard's point-lock wiring."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from siyi_sdk.tracking import LockState, LockStatus  # noqa: E402
from web_ui.lock_overlay import draw_lock  # noqa: E402


@pytest.fixture(scope="module")
def ground() -> np.ndarray:
    rng = np.random.default_rng(5)
    noise = (rng.random((720, 1600, 3)) * 255).astype(np.uint8)
    return cv2.GaussianBlur(noise, (0, 0), 2.5)


class FakeClient:
    def __init__(self) -> None:
        self.rotations: list[tuple[int, int]] = []
        self.zooms: list[float] = []

    async def rotate_nowait(self, yaw: int, pitch: int) -> None:
        self.rotations.append((yaw, pitch))

    async def absolute_zoom(self, zoom: float) -> None:
        self.zooms.append(zoom)


@pytest.fixture
def camera(ground):
    from web_ui import server

    state = server.CameraState()
    state.client = FakeClient()
    state.is_connected = True
    state.latest_image = ground[:, :1280].copy()
    yield state
    state.motion_deadlines.clear()


def frame(image: np.ndarray, t: float) -> SimpleNamespace:
    return SimpleNamespace(frame=image, timestamp=t)


async def test_lock_steers_and_draws(camera, ground) -> None:
    # Lock a spot to the right of centre: the gimbal should turn right.
    camera.preview_viewers = 1  # the preview is only encoded while a browser is watching
    snapshot = await camera.start_lock(0.25, 0.0)
    assert snapshot["state"] == "locked"
    for t in range(3):
        await camera._on_frame(frame(ground[:, :1280].copy(), time.monotonic() + t * 0.033))
    assert camera.client.rotations and camera.client.rotations[-1][0] > 0
    snap = camera.lock_snapshot()
    assert snap["state"] == "locked" and snap["x"] == pytest.approx(0.25, abs=0.01)
    assert snap["error_deg"][0] > 0
    if camera.preview_task:
        await camera.preview_task
    assert camera.latest_frame  # the marked frame was encoded for the browser


async def test_scene_moving_left_moves_the_spot_left(camera, ground) -> None:
    await camera.start_lock(0.0, 0.0)
    for t, dx in enumerate(range(0, 120, 12)):
        await camera._on_frame(
            frame(ground[:, dx : dx + 1280].copy(), time.monotonic() + t * 0.033)
        )
    # The view slid right by 108 px, so the locked spot moved 108 px left.
    assert camera.lock_snapshot()["x"] == pytest.approx(-108 / 1280, abs=0.004)


async def test_release_stops_the_gimbal(camera, ground) -> None:
    await camera.start_lock(0.3, 0.0)
    await camera._on_frame(frame(ground[:, :1280].copy(), time.monotonic()))
    await camera.release_lock()
    assert camera.client.rotations[-1] == (0, 0)
    assert camera.lock_snapshot() == {"state": "idle"}


async def test_centred_lock_does_not_flood_stop_commands(camera, ground) -> None:
    await camera.start_lock(0.0, 0.0)
    for t in range(10):
        await camera._on_frame(frame(ground[:, :1280].copy(), time.monotonic() + t * 0.033))
    # One stop (sent three times by the dashboard's stop path), not three per frame.
    assert len(camera.client.rotations) <= 3


async def test_lock_needs_video(camera) -> None:
    from fastapi import HTTPException

    camera.latest_image = None
    with pytest.raises(HTTPException):
        await camera.start_lock(0.0, 0.0)


@pytest.mark.parametrize("on_screen", [True, False])
def test_overlay_draws_marker_or_arrow(on_screen) -> None:
    image = np.zeros((360, 640, 3), np.uint8)
    x = 320 if on_screen else 1200
    status = LockStatus(LockState.LOCKED, x, 180, 640, 360, 0.9, on_screen=on_screen)
    draw_lock(image, status)
    assert image.any()
    blank = np.zeros_like(image)
    draw_lock(blank, LockStatus(LockState.IDLE))
    assert not blank.any()


async def test_lock_uses_angle_targets_when_attitude_is_streaming(camera, ground) -> None:
    angles: list[tuple[float, float]] = []

    async def set_attitude_nowait(yaw: float, pitch: float) -> None:
        angles.append((yaw, pitch))

    camera.client.set_attitude_nowait = set_attitude_nowait
    now = time.monotonic()
    for i in range(40):  # half a second of a still gimbal at 80 Hz
        camera.attitude_history.add(now - 0.5 + i / 80, 0.0, 0.0)
    camera.preview_viewers = 1  # the preview is only encoded while a browser is watching
    snapshot = await camera.start_lock(0.25, 0.0)
    assert snapshot["state"] == "locked"
    assert camera.point_lock.control == "angle" and camera.point_lock.compensated
    for _ in range(3):
        camera.attitude_history.add(time.monotonic(), 0.0, 0.0)
        await camera._on_frame(frame(ground[:, :1280].copy(), time.monotonic()))
        await asyncio.sleep(0.05)  # let the 50 Hz control loop run
    assert angles and angles[-1][0] > 5  # spot is a quarter-frame right: aim right
    assert camera.lock_snapshot()["compensated"] is True
    await camera.release_lock()


async def test_calibration_needs_live_video(camera) -> None:
    from fastapi import HTTPException

    camera.latest_image = None
    with pytest.raises(HTTPException):
        await camera.calibrate()


def test_pointing_config_rejects_zero_turn_rate() -> None:
    from pydantic import ValidationError

    from web_ui.server import PointingConfigRequest

    base = {"hfov_deg": 81, "yaw_sign": 1, "pitch_sign": 1, "video_delay_ms": 200}
    PointingConfigRequest(**base)  # settings saved by older dashboards still load
    with pytest.raises(ValidationError):
        PointingConfigRequest(**base, deg_per_unit_yaw=0.0)
