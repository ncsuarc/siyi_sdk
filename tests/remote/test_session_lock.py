"""Point lock run by the camera service, steered from the ground through RemoteCamera."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
import pytest_asyncio

from siyi_sdk import SIYIClient, exceptions
from siyi_sdk.remote import (
    CameraServer,
    CameraSession,
    ConflictError,
    LockConfig,
    RemoteCamera,
    ServiceUnavailableError,
)
from siyi_sdk.remote.lock_config import mounted_inverted
from siyi_sdk.tracking.attitude import AttitudeHistory
from tests.remote.fake_camera import FakeCamera
from tests.remote.test_server import wait_for
from tests.tracking.sim import HFOV, PX_PER_DEG, H, W, make_ground

GROUND = cv2.cvtColor(make_ground(), cv2.COLOR_BGR2GRAY)


def view(yaw: float, pitch: float) -> np.ndarray:
    """What the camera sees pointing at (yaw, pitch); positive yaw right, pitch up."""
    cx = 2000 + yaw * PX_PER_DEG
    cy = 1200 - pitch * PX_PER_DEG
    m = np.float32([[1, 0, W / 2 - cx], [0, 1, H / 2 - cy]])
    return cv2.warpAffine(GROUND, m, (W, H), flags=cv2.INTER_LINEAR)


class Rig:
    """Fake camera plus a 30 fps video feed of what it points at."""

    def __init__(self) -> None:
        self.camera = FakeCamera()
        self.client = SIYIClient(self.camera, default_timeout=0.3, max_retries=0)
        # The fake gimbal reaches 0x0E targets at once and frames arrive undelayed.
        config = LockConfig(hfov_deg=HFOV, frame_delay_ms=0.0, calibrated=True)
        config.ground_extra_video_delay_ms = 0.0
        self.session = CameraSession(client=self.client, config=config, video="none")
        self.server = CameraServer(self.session, host="127.0.0.1", port=0)
        self.frames: dict[int, float] = {}  # id(image) -> timestamp
        self._pump: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.server.port}"

    async def start(self) -> None:
        await self.server.start()
        await wait_for(lambda: self.session.camera_alive)
        self._pump = asyncio.create_task(self._feed())
        await wait_for(lambda: self.session.video_live)

    async def stop(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            await asyncio.gather(self._pump, return_exceptions=True)
        await self.server.close()
        await self.client.close()

    async def _feed(self) -> None:
        while True:
            image = view(self.camera.yaw, self.camera.pitch)
            stamp = time.monotonic()
            self.frames[id(image)] = stamp
            await self.session.on_frame(image, stamp, source="test")
            await asyncio.sleep(1 / 30)


@pytest_asyncio.fixture
async def rig() -> AsyncIterator[Rig]:
    rig = Rig()
    await rig.start()
    try:
        yield rig
    finally:
        await rig.stop()


async def test_lock_steers_the_gimbal_onto_the_spot(rig: Rig) -> None:
    statuses: list[dict[str, Any]] = []
    async with RemoteCamera(rig.url) as cam:
        cam.on_lock_status(statuses.append)
        start_yaw, start_pitch = rig.camera.yaw, rig.camera.pitch
        # A spot 0.15 of the width right of centre and 0.1 of the height above it.
        snapshot = await cam.lock(0.15, -0.1)
        assert snapshot["state"] == "locked"
        assert snapshot["controller"] == "angle"
        spot_yaw = start_yaw + 0.15 * W / PX_PER_DEG
        spot_pitch = start_pitch + 0.1 * H / PX_PER_DEG

        def settled() -> bool:
            return abs(rig.camera.yaw - spot_yaw) < 1.0 and abs(rig.camera.pitch - spot_pitch) < 1.0

        await wait_for(settled, timeout=4.0)
        assert any(s["state"] == "locked" for s in statuses)
        final = await cam.release()
        assert final["state"] == "idle"
    assert statuses[-1]["state"] == "idle"


async def test_manual_rate_releases_the_lock(rig: Rig) -> None:
    async with RemoteCamera(rig.url) as cam:
        await cam.lock(0.0, 0.0)
        cam.rate(20, 0)
        await wait_for(lambda: rig.session.lock is None)
        status = await cam.get_lock()
        assert status["state"] == "idle"
        assert "manual" in status["reason"]
        cam.rate(0, 0)


async def test_frame_time_selects_the_clicked_frame(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    used: list[float] = []
    build = rig.session.config.build_app_lock

    def spy(**kwargs: Any) -> Any:
        lock = build(**kwargs)
        original = lock.lock

        def record(frame: np.ndarray, *args: Any) -> None:
            used.append(rig.frames[id(frame)])
            original(frame, *args)

        lock.lock = record  # type: ignore[method-assign]
        return lock

    monkeypatch.setattr(rig.session.config, "build_app_lock", spy)
    await asyncio.sleep(0.5)  # fill the frame buffer
    async with RemoteCamera(rig.url) as cam:
        await cam.ping()
        clicked = time.monotonic() - 0.3
        await cam.lock(0.0, 0.0, frame_time=clicked)
        assert abs(used[-1] - clicked) < 0.05
        await cam.lock(0.0, 0.0)  # no frame time: the newest frame
        assert time.monotonic() - used[-1] < 0.1
        await cam.release()


async def test_firmware_lock_needs_supported_firmware(rig: Rig) -> None:
    async with RemoteCamera(rig.url) as cam:
        with pytest.raises(ServiceUnavailableError, match="firmware"):
            await cam.lock(0.0, 0.0, controller="firmware")
        with pytest.raises(exceptions.ConfigurationError):
            await cam.lock(0.0, 0.0, controller="joystick")  # type: ignore[arg-type]


# ============================================================================
# LockConfig
# ============================================================================


def test_config_saves_and_loads(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "lock.json"
    config = LockConfig(control="rate", deg_per_unit_yaw=0.68)
    config.store_mount("normal")
    config.save(path)
    loaded = LockConfig.load(path)
    assert loaded == config
    assert LockConfig.load(tmp_path / "missing.json") == LockConfig()
    assert [p.name for p in path.parent.iterdir()] == ["lock.json"]  # no temp files left


def test_config_rejects_bad_values() -> None:
    config = LockConfig()
    for bad in ({"control": "joystick"}, {"yaw_sign": 2}, {"hfov_deg": "wide"}, {"bogus": 1}):
        with pytest.raises(exceptions.ConfigurationError):
            config.update(bad)
    assert config == LockConfig()  # nothing half-applied


def test_mount_profiles_refuse_to_guess() -> None:
    config = LockConfig(yaw_sign=-1)
    config.store_mount("normal")
    config.yaw_sign = 1
    config.apply_mount("normal")
    assert config.yaw_sign == -1
    with pytest.raises(ConflictError, match="upside down"):
        config.apply_mount("inverted")
    assert mounted_inverted(170.0) and mounted_inverted(-170.0)
    assert not mounted_inverted(-90.0) and not mounted_inverted(20.0)


def test_app_lock_follows_the_settings() -> None:
    async def send_rate(yaw: int, pitch: int) -> None: ...
    async def send_angle(yaw: float, pitch: float) -> None: ...

    config = LockConfig(response=0.5, max_speed=60, calibrated=True, control="angle")
    history = AttitudeHistory()
    kwargs: dict[str, Any] = {
        "send_rate": send_rate,
        "send_angle": send_angle,
        "attitude": history,
        "turn_rate": lambda loop: None,
    }
    # Without attitude telemetry there is nothing to compensate the delay with.
    blind = config.build_app_lock(control="angle", inverted=False, **kwargs)
    assert blind.control == "rate"
    history.add(time.monotonic(), 0.0, -10.0)
    lock = config.build_app_lock(control="angle", inverted=True, **kwargs)
    assert lock.control == "angle"
    assert lock.controller.gains.max_speed == 60
    reference = config.build_app_lock(control="angle", inverted=False, **kwargs)
    assert reference.loop == config.loop_model()
