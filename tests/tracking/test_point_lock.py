"""Tests for PointLock and GimbalPointLock on synthetic camera motion."""

from __future__ import annotations

import math

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from siyi_sdk.tracking import GimbalPointLock, LockGains, LockState, PointLock  # noqa: E402

W, H = 640, 360


@pytest.fixture(scope="module")
def ground() -> np.ndarray:
    """A large textured 'ground' image to fly a virtual camera over."""
    rng = np.random.default_rng(3)
    noise = (rng.random((1200, 2000, 3)) * 255).astype(np.uint8)
    return cv2.GaussianBlur(noise, (0, 0), 2.5)


def view(ground: np.ndarray, cx: float, cy: float, yaw_deg: float = 0.0, s: float = 1.0):
    """Camera image centred on ground point (cx, cy), rotated and scaled; returns (frame, M)."""
    c, si = math.cos(math.radians(yaw_deg)) * s, math.sin(math.radians(yaw_deg)) * s
    a = np.array([[c, -si], [si, c]])
    b = np.array([W / 2, H / 2]) - a @ np.array([cx, cy])
    m = np.hstack([a, b[:, None]])
    return cv2.warpAffine(ground, m, (W, H), flags=cv2.INTER_LINEAR), m


@pytest.mark.parametrize("model", ["local", "global"])
def test_follows_ground_point_through_pan_rotation_and_zoom(ground, model) -> None:
    target = np.array([1000.0, 600.0])
    frame, _ = view(ground, 1000, 600)
    lock = PointLock(model)
    lock.init(frame, (W // 2 - 20, H // 2 - 20, 40, 40))
    errors = []
    for t in range(1, 90):
        k = t / 90
        frame, m = view(ground, 1000 + 250 * k, 600 - 120 * k, yaw_deg=10 * k, s=1 + 0.3 * k)
        ok, _ = lock.update(frame)
        assert ok and lock.getTrackingScore() > 0.5
        truth = m @ np.append(target, 1)
        errors.append(math.dist(lock.center(), truth))
    # The point ends up off-centre (the camera panned away) yet stays accurate.
    assert max(errors) < 4.0


def test_reports_loss_on_featureless_frames(ground) -> None:
    frame, _ = view(ground, 1000, 600)
    lock = PointLock()
    lock.init(frame, (300, 160, 40, 40))
    ok, _ = lock.update(np.full_like(frame, 128))
    assert not ok and lock.getTrackingScore() == 0.0


def test_rejects_unknown_model() -> None:
    with pytest.raises(ValueError):
        PointLock("affine")  # type: ignore[arg-type]


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int]] = []

    async def __call__(self, yaw: int, pitch: int) -> None:
        self.sent.append((yaw, pitch))


async def test_gimbal_lock_steers_toward_the_spot(ground) -> None:
    send = Recorder()
    lock = GimbalPointLock(send=send, hfov_deg=80, gains=LockGains(kp=2, ki=0))
    frame, _ = view(ground, 1000, 600)
    # Lock a spot right of and above centre: the gimbal should turn right and up.
    lock.lock(frame, x=W / 2 + 160, y=H / 2 - 90)
    status = await lock.update(frame, timestamp=0.0)
    assert status.state is LockState.LOCKED
    assert status.error_deg[0] > 0 and status.error_deg[1] > 0
    assert send.sent[-1][0] > 0 and send.sent[-1][1] > 0
    await lock.release()
    assert send.sent[-1] == (0, 0) and not lock.active


async def test_gimbal_lock_holds_still_then_releases_when_lost(ground) -> None:
    send = Recorder()
    lock = GimbalPointLock(send=send, lost_timeout=1.0)
    frame, _ = view(ground, 1000, 600)
    lock.lock(frame, x=W / 2, y=H / 2)
    blank = np.full_like(frame, 128)
    status = await lock.update(blank, timestamp=0.0)
    assert status.state is LockState.SEARCHING and send.sent[-1] == (0, 0)
    status = await lock.update(blank, timestamp=0.5)
    assert status.state is LockState.SEARCHING
    status = await lock.update(blank, timestamp=1.6)
    assert status.state is LockState.IDLE and not lock.active


def test_gimbal_lock_needs_a_way_to_send() -> None:
    with pytest.raises(ValueError):
        GimbalPointLock()


def test_angle_control_needs_attitude() -> None:
    async def send(yaw: float, pitch: float) -> None: ...

    with pytest.raises(ValueError):
        GimbalPointLock(send=send, send_angle=send, control="angle")
