"""Fixed-point optical flow with the A8 Mini's experimental firmware controller."""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections.abc import Awaitable, Callable

from siyi_sdk.firmware_tracking import (
    CANCEL_SETTLE,
    CONNECT_TIMEOUT,
    IO_TIMEOUT,
    REPLY_TIMEOUT,
    FirmwareTrackingClient,
)
from siyi_sdk.tracking.attitude import AttitudeHistory
from siyi_sdk.tracking.control import pixel_error_deg
from siyi_sdk.tracking.gimbal import _ATTITUDE_STALE_S, LockState, LockStatus
from siyi_sdk.tracking.point_lock import Image, PointLock, PointModel

# Integrate the offset only within this fraction of the frame width of the centre, and
# let the integral term shift the target by at most this fraction of the width.
_INTEGRAL_ZONE = 0.05
_INTEGRAL_LIMIT = 0.08


class FirmwarePointLock:
    """Send observed scene coordinates; the gimbal does all steering and zoom compensation.

    Loss, off-screen points, stale video and connection failure release the lock.
    So does AI mode being switched off behind its back: the camera answers a target with
    ``02`` then, which the client treats as a failed connection; the lock does not poll
    the mode (the SIYI AI module never does). Each instance is single-use. It never
    replays targets or falls back to the app controller, and reconnects only to switch
    AI mode off after the camera closed the channel. Like the AI module, it ends a lock
    with a "canceled" target before switching AI mode off. ``stop`` is only an emergency
    public-SDK stop.
    """

    control = "firmware"

    def __init__(
        self,
        client: FirmwareTrackingClient,
        *,
        stop: Callable[[], Awaitable[None]],
        model: PointModel = "local",
        min_score: float = 0.2,
        send_interval: float = 0.05,
        attitude: AttitudeHistory | None = None,
        attitude_signs: tuple[int, int] = (1, 1),
        video_delay_s: float = 0.2,
        hfov_deg: float = 81.0,
        response: float = 1.0,
        integral: float = 1.5,
        owns_connection: bool = True,
        video_timeout: float = IO_TIMEOUT,
    ) -> None:
        """Create an idle controller using the existing fixed-scene point tracker.

        ``send_interval`` limits target writes (default 20 Hz, near the SIYI AI
        module's per-frame rate); the tracker still processes every frame. The
        firmware keeps steering toward the last target, so slow updates (2 Hz)
        made it overshoot into a growing oscillation.

        With ``attitude``, each target is shifted by the gimbal's turn since the
        frame was captured (``video_delay_s`` before arrival). Otherwise the
        firmware keeps steering toward where the point was, and overshoots.
        It relies on correct attitude signs and video delay; a wrong sign makes
        the gimbal run away to a limit.

        ``response`` below 1 sends the point that fraction of the way from the
        image centre, so the firmware steers more gently; above 1 it exaggerates
        the offset (clamped to the frame), so it steers harder. It needs no
        measured values, and the point still converges to the centre.

        The firmware leaves a small standing offset and lags a point that keeps
        drifting (the camera translating). ``integral`` (1/s) adds the offset's
        running sum to the target, accumulated only near the centre so a large
        initial error can't wind it up; 0 disables it.
        """
        self.client = client
        self._stop = stop
        self._model = model
        self._min_score = min_score
        self._send_interval = send_interval
        self._last_send = 0.0
        self._attitude = attitude
        self._signs = attitude_signs
        self._video_delay = video_delay_s
        self._hfov = hfov_deg
        self._zoom = 1.0
        self._response = response
        self._ki = integral
        self._integral = [0.0, 0.0]  # pixels x seconds, in the decoded image
        # False when a FirmwareLink keeps ``client`` connected: the lock then neither
        # connects nor closes it, and only switches AI mode.
        self._owns_connection = owns_connection
        # Release after this long without a processed frame. The camera's own stream can
        # arrive in bursts; meanwhile its controller keeps steering to the last target.
        self._video_timeout = video_timeout
        self._tracker: PointLock | None = None
        self._lifecycle = asyncio.Lock()
        self._update_lock = asyncio.Lock()
        self._generation = 0
        self._needs_disable = False
        self._used = False
        # Whether a target has gone out, so there is something to cancel (and AI mode is on).
        self._target_sent = False
        self._last_frame = 0.0
        self._last_timestamp = 0.0
        self._watchdog: asyncio.Task[None] | None = None
        self._cleanup: asyncio.Task[None] | None = None
        self.status = LockStatus(LockState.IDLE)
        self.reason: str | None = None
        self.exit_confirmed = True
        # Last target written to the camera, (x, y, width, height) in its 1280x720 space,
        # and when; for showing the commanded correction on the video.
        self.last_target: tuple[int, int, int, int] | None = None
        self.last_target_time = 0.0

    @property
    def tracker(self) -> PointLock | None:
        """The fixed-scene tracker while locked, for drawing its debug view."""
        return self._tracker

    @property
    def active(self) -> bool:
        """Whether a point is locked after confirmed AI-mode activation."""
        return self.status.state is LockState.LOCKED

    async def start(
        self,
        frame: Image,
        x: float,
        y: float,
        *,
        size: int = 60,
        timestamp: float | None = None,
        zoom: float = 1.0,
    ) -> None:
        """Initialize a point and confirm firmware mode before sending coordinates."""
        if self._used:
            raise RuntimeError("Create a new FirmwarePointLock for each lock")
        self._used = True
        self._zoom = zoom
        generation = self._generation
        self._last_frame = time.monotonic()
        self._last_timestamp = timestamp if timestamp is not None else self._last_frame
        height, width = frame.shape[:2]
        if not (math.isfinite(x) and math.isfinite(y) and 0 <= x < width and 0 <= y < height):
            raise ValueError("Choose a point inside the video")
        tracker = PointLock(model=self._model)
        await asyncio.to_thread(
            tracker.init, frame, (round(x - size / 2), round(y - size / 2), size, size)
        )
        async with self._lifecycle:
            try:
                if generation != self._generation:
                    raise RuntimeError("Point lock cancelled during initialization")
                self.client.note("lock_start", x=round(x), y=round(y), size=size, zoom=zoom,
                                 frame=f"{width}x{height}", response=self._response,
                                 send_interval=self._send_interval)
                if self._owns_connection:
                    await self.client.connect()
                elif not self.client.is_connected:
                    raise ConnectionError("Firmware link is not connected")
                self._needs_disable = True  # a lost acknowledgement can still mean enabled
                if await self.client.get_mode():
                    # Left on by an earlier lock whose exit was never confirmed (the camera
                    # keeps AI mode across disconnects). This assumes no AI module is also
                    # steering; with one attached, this takes control away from it.
                    self.client.note("ai_mode_was_left_on")
                    await self.client.set_mode(False)
                self.exit_confirmed = False
                await self.client.set_mode(True)
                if (
                    generation != self._generation
                    # Connecting can take most of CONNECT_TIMEOUT. The gimbal was stopped
                    # before the lock started, so the start frame's point is still valid.
                    or time.monotonic() - self._last_timestamp
                    >= CONNECT_TIMEOUT + 2 * REPLY_TIMEOUT
                ):
                    raise RuntimeError("Point lock cancelled or video stale during activation")
                self._tracker = tracker
                self.status = LockStatus(LockState.LOCKED, x, y, width, height, score=1.0)
                await self._send(tracker, width, height)
                # No frames are processed during activation; time video freshness from now.
                self._last_frame = time.monotonic()
                self._watchdog = asyncio.create_task(self._watch())
                self.client.note("locked")
            except BaseException as exc:
                self.client.note("lock_start_failed", error=repr(exc))
                self._tracker = None
                self.status = LockStatus(LockState.IDLE)
                await self._disable()
                raise

    def _predict(self, x: float, y: float, width: int, height: int) -> tuple[float, float]:
        """Move a captured-frame point to where the gimbal's turn since capture puts it now."""
        history = self._attitude
        latest = history.latest() if history is not None else None
        if history is None or latest is None or time.monotonic() - latest[0] > _ATTITUDE_STALE_S:
            return x, y
        then = history.at(self._last_timestamp - self._video_delay)
        if then is None:
            return x, y
        turned_yaw = (latest[1] - then[0]) * self._signs[0]
        turned_pitch = (latest[2] - then[1]) * self._signs[1]
        yaw, pitch = pixel_error_deg(x, y, width, height, hfov_deg=self._hfov, zoom=self._zoom)
        tan_half = math.tan(math.radians(self._hfov) / 2) / self._zoom
        yaw, pitch = yaw - turned_yaw, pitch - turned_pitch
        if max(abs(yaw), abs(pitch)) >= 89:
            return x, y
        return (
            width * (1 + math.tan(math.radians(yaw)) / tan_half) / 2,
            height * (1 - math.tan(math.radians(pitch)) / (tan_half * height / width)) / 2,
        )

    async def _send(self, tracker: PointLock, width: int, height: int) -> None:
        x, y = self._predict(*tracker.center(), width, height)
        now = time.monotonic()
        dt = min(now - self._last_send, 0.2) if self._target_sent else 0.0
        offsets = []
        for axis, (value, size) in enumerate(((x, width), (y, height))):
            error = value - size / 2
            if self._ki and abs(error) < width * _INTEGRAL_ZONE:
                self._integral[axis] += error * dt
                limit = width * _INTEGRAL_LIMIT / self._ki
                self._integral[axis] = max(-limit, min(limit, self._integral[axis]))
            offsets.append(error * self._response + self._ki * self._integral[axis])
        x, y = width / 2 + offsets[0], height / 2 + offsets[1]
        _, _, box_width, box_height = tracker.box()
        self._last_send = now
        # Current decoded-image coordinates already include the user's zoom.
        target = (
            min(1279, max(0, round(x * 1280 / width))),
            min(719, max(0, round(y * 720 / height))),
            min(1280, max(1, round(box_width * 1280 / width))),
            min(720, max(1, round(box_height * 720 / height))),
        )
        with contextlib.suppress(Exception):  # diagnostics must never break steering
            self.client.note(
                "target", tracked=[round(v) for v in tracker.center()], sent=list(target),
                score=round(float(tracker.getTrackingScore()), 3),
                video_age_ms=round((time.monotonic() - self._last_timestamp) * 1000),
            )
        await self.client.send_target(*target)
        self._target_sent = True
        self.last_target, self.last_target_time = target, time.monotonic()

    async def update(
        self, frame: Image, *, zoom: float = 1.0, timestamp: float | None = None
    ) -> LockStatus:
        """Process one fresh frame, dropping concurrent updates instead of queueing them."""
        if not self.active or self._update_lock.locked():
            return self.status
        async with self._update_lock:
            tracker = self._tracker
            if tracker is None:
                return self.status
            generation = self._generation
            received = time.monotonic()
            stamp = timestamp if timestamp is not None else received
            if (
                timestamp is not None and stamp <= self._last_timestamp
            ) or received - stamp >= IO_TIMEOUT:
                await self.release("Point lock released: video is stale")
                return self.status
            self._last_frame, self._last_timestamp = received, stamp
            self._zoom = zoom
            height, width = frame.shape[:2]
            if (width, height) != (self.status.width, self.status.height):
                await self.release("Point lock released: video dimensions changed")
                return self.status
            try:
                ok, _ = await asyncio.to_thread(tracker.update, frame)
                if generation != self._generation:
                    return self.status
                x, y = tracker.center()
                score = tracker.getTrackingScore()
                if not ok or not math.isfinite(score) or score < self._min_score:
                    await self.release("Point lock released: tracking confidence was lost")
                elif not (
                    math.isfinite(x) and math.isfinite(y) and 0 <= x < width and 0 <= y < height
                ):
                    await self.release("Point lock released: the point left the screen")
                elif time.monotonic() - stamp >= IO_TIMEOUT:
                    await self.release("Point lock released: video processing is stale")
                elif time.monotonic() - self._last_send < self._send_interval:
                    self.status = LockStatus(LockState.LOCKED, x, y, width, height, score)
                else:
                    async with self._lifecycle:
                        if generation == self._generation:
                            if time.monotonic() - stamp >= IO_TIMEOUT:
                                raise RuntimeError("Video became stale before target transmission")
                            await self._send(tracker, width, height)
                            if generation == self._generation:
                                self.status = LockStatus(
                                    LockState.LOCKED, x, y, width, height, score
                                )
            except Exception as exc:
                await self.release(f"Firmware point lock failed: {exc}")
        return self.status

    async def _watch(self) -> None:
        while self.active:
            await asyncio.sleep(0.025)
            if not self.client.is_connected:
                await self.release(self.client.error or "Tracking connection lost")
            elif time.monotonic() - self._last_frame >= self._video_timeout:
                await self.release(
                    f"Point lock released: no fresh video for {self._video_timeout * 1000:.0f} ms"
                )

    async def release(self, reason: str = "Point lock released") -> None:
        """Invalidate pending work, disable AI, confirm exit, then close the private channel."""
        self._generation += 1
        self._tracker = None
        self.status = LockStatus(LockState.IDLE)
        self.reason = self.reason or reason
        if self._cleanup is None:
            self.client.note("release", reason=reason)
            self._cleanup = asyncio.create_task(self._finish_release())
        try:
            await asyncio.shield(self._cleanup)
        except asyncio.CancelledError:
            await asyncio.shield(self._cleanup)
            raise

    async def _finish_release(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
        self._watchdog = None
        async with self._lifecycle:
            await self._disable()

    async def _disable(self) -> None:
        if self._needs_disable:
            self._needs_disable = False
            try:
                if self._target_sent:
                    # End as the AI module does: a "canceled" target, then AI mode off. A dead
                    # channel has nothing to cancel, so any failure here is ignored.
                    self._target_sent = False
                    with contextlib.suppress(Exception):
                        await self.client.cancel_target()
                        await asyncio.sleep(CANCEL_SETTLE)
                try:
                    await self.client.set_mode(False)
                except Exception as exc:
                    # AI mode survives disconnects and the camera can stall briefly, so try
                    # once more; if the camera closed the channel, open a fresh one first.
                    self.client.note("disable_retry", error=repr(exc),
                                     connected=self.client.is_connected)
                    if not self.client.is_connected:
                        if not self._owns_connection:
                            raise  # the link switches AI mode off when it reconnects
                        await self.client.close()
                        await self.client.connect()
                    await self.client.set_mode(False)
                self.exit_confirmed = True
                self.client.note("ai_mode_off_confirmed")
            except Exception as exc:
                self.client.note("ai_mode_off_failed", error=repr(exc))
                self.exit_confirmed = False
                self.reason = (
                    f"{self.reason or 'Tracking failed'}. "
                    f"Firmware exit is unconfirmed ({exc or type(exc).__name__}); "
                    "public stop attempted"
                )
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._stop(), IO_TIMEOUT)
        if self._owns_connection:
            with contextlib.suppress(Exception):
                await self.client.close()
