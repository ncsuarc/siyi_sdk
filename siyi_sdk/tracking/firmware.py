"""Fixed-point optical flow with the A8 Mini's experimental firmware controller."""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections.abc import Awaitable, Callable

from siyi_sdk.firmware_tracking import IO_TIMEOUT, FirmwareTrackingClient
from siyi_sdk.tracking.gimbal import LockState, LockStatus
from siyi_sdk.tracking.point_lock import Image, PointLock, PointModel


class FirmwarePointLock:
    """Send observed scene coordinates; the gimbal does all steering and zoom compensation.

    Loss, off-screen points, stale video and connection failure release the lock.
    Each instance is single-use. It never reconnects, replays targets or falls
    back to the app controller. ``stop`` is only an emergency public-SDK stop.
    """

    control = "firmware"

    def __init__(
        self,
        client: FirmwareTrackingClient,
        *,
        stop: Callable[[], Awaitable[None]],
        model: PointModel = "local",
        min_score: float = 0.2,
    ) -> None:
        """Create an idle controller using the existing fixed-scene point tracker."""
        self.client = client
        self._stop = stop
        self._model = model
        self._min_score = min_score
        self._tracker: PointLock | None = None
        self._lifecycle = asyncio.Lock()
        self._update_lock = asyncio.Lock()
        self._generation = 0
        self._needs_disable = False
        self._used = False
        self._last_frame = 0.0
        self._last_timestamp = 0.0
        self._watchdog: asyncio.Task[None] | None = None
        self._health: asyncio.Task[None] | None = None
        self._cleanup: asyncio.Task[None] | None = None
        self.status = LockStatus(LockState.IDLE)
        self.reason: str | None = None
        self.exit_confirmed = True

    @property
    def active(self) -> bool:
        """Whether a point is locked after confirmed AI-mode activation."""
        return self.status.state is LockState.LOCKED

    async def start(
        self, frame: Image, x: float, y: float, *, size: int = 60, timestamp: float | None = None
    ) -> None:
        """Initialize a point and confirm firmware mode before sending coordinates."""
        if self._used:
            raise RuntimeError("Create a new FirmwarePointLock for each lock")
        self._used = True
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
                await self.client.connect()
                if await self.client.get_mode():
                    raise RuntimeError(
                        "Firmware AI tracking is already active; "
                        "release its current controller first"
                    )
                self._needs_disable = True  # a lost acknowledgement can still mean enabled
                self.exit_confirmed = False
                await self.client.set_mode(True)
                if (
                    generation != self._generation
                    or time.monotonic() - self._last_timestamp >= IO_TIMEOUT
                ):
                    raise RuntimeError("Point lock cancelled or video stale during activation")
                self._tracker = tracker
                self.status = LockStatus(LockState.LOCKED, x, y, width, height, score=1.0)
                await self._send(tracker, width, height)
                self._watchdog = asyncio.create_task(self._watch())
                self._health = asyncio.create_task(self._check_mode())
            except BaseException:
                self._tracker = None
                self.status = LockStatus(LockState.IDLE)
                await self._disable()
                raise

    async def _send(self, tracker: PointLock, width: int, height: int) -> None:
        x, y = tracker.center()
        _, _, box_width, box_height = tracker.box()
        # Current decoded-image coordinates already include the user's zoom.
        await self.client.send_target(
            min(1279, max(0, round(x * 1280 / width))),
            min(719, max(0, round(y * 720 / height))),
            min(1280, max(1, round(box_width * 1280 / width))),
            min(720, max(1, round(box_height * 720 / height))),
        )

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
            elif time.monotonic() - min(self._last_frame, self._last_timestamp) >= IO_TIMEOUT:
                await self.release("Point lock released: no fresh video for 400 ms")

    async def _check_mode(self) -> None:
        while self.active:
            await asyncio.sleep(0.2)
            try:
                if not await self.client.get_mode():
                    raise RuntimeError("Camera left AI tracking mode")
            except Exception as exc:
                await self.release(f"Tracking mode check failed: {exc}")

    async def release(self, reason: str = "Point lock released") -> None:
        """Invalidate pending work, disable AI, confirm exit, then close the private channel."""
        self._generation += 1
        self._tracker = None
        self.status = LockStatus(LockState.IDLE)
        self.reason = self.reason or reason
        if self._cleanup is None:
            self._cleanup = asyncio.create_task(self._finish_release())
        try:
            await asyncio.shield(self._cleanup)
        except asyncio.CancelledError:
            await asyncio.shield(self._cleanup)
            raise

    async def _finish_release(self) -> None:
        for task in (self._watchdog, self._health):
            if task is not None:
                task.cancel()
        self._watchdog = self._health = None
        async with self._lifecycle:
            await self._disable()

    async def _disable(self) -> None:
        if self._needs_disable:
            self._needs_disable = False
            try:
                await self.client.set_mode(False)
                self.exit_confirmed = True
            except Exception:
                self.exit_confirmed = False
                self.reason = (
                    f"{self.reason or 'Tracking failed'}. "
                    "Firmware exit is unconfirmed; public stop attempted"
                )
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._stop(), IO_TIMEOUT)
        with contextlib.suppress(Exception):
            await self.client.close()
