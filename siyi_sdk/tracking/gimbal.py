# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Keep the gimbal pointed at a locked spot using the camera's own video."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

from siyi_sdk.tracking.control import LockGains, RateController, pixel_error_deg
from siyi_sdk.tracking.point_lock import PointLock, PointModel

if TYPE_CHECKING:
    from siyi_sdk.client import SIYIClient

SendRate = Callable[[int, int], Awaitable[None]]


class LockState(str, Enum):
    """Where a :class:`GimbalPointLock` is."""

    IDLE = "idle"  # nothing locked
    LOCKED = "locked"  # tracking and steering
    SEARCHING = "searching"  # scene motion lost; holding still until it recovers or times out


@dataclass(frozen=True)
class LockStatus:
    """Snapshot after an update.

    Attributes:
        state: Current lock state.
        x: Locked spot in frame pixels (may be outside the frame).
        y: Locked spot in frame pixels.
        width: Frame width the position refers to.
        height: Frame height the position refers to.
        score: Fraction of scene features agreeing with the motion estimate.
        error_deg: (yaw, pitch) angle from image centre to the spot.
        command: (yaw, pitch) rotation speed sent this update.
        on_screen: Whether the spot is inside the frame.
    """

    state: LockState
    x: float = 0.0
    y: float = 0.0
    width: int = 0
    height: int = 0
    score: float = 0.0
    error_deg: tuple[float, float] = (0.0, 0.0)
    command: tuple[int, int] = (0, 0)
    on_screen: bool = True


class GimbalPointLock:
    """Lock onto a spot in the video and steer the gimbal to keep it centred.

    Feed every decoded frame to :meth:`update`. Each update moves the locked
    spot with the scene (see :class:`PointLock`), converts its offset from the
    image centre to an angle, and sends a rotation-speed command (0x07). Speed
    commands are used rather than angle targets because the A8 Mini reports yaw
    relative to the aircraft body, which goes stale if the aircraft turns during
    the video delay; a speed loop on the image error doesn't care.

    Use Lock gimbal mode so aircraft rotation is already stabilized and the loop
    only corrects for aircraft movement.

    Example:
        >>> lock = GimbalPointLock(client, hfov_deg=81)
        >>> lock.lock(frame.frame, x=640, y=360)
        >>> status = await lock.update(next_frame.frame, zoom=2.0)
        >>> await lock.release()
    """

    def __init__(
        self,
        client: SIYIClient | None = None,
        *,
        hfov_deg: float = 81.0,
        model: PointModel = "local",
        gains: LockGains | None = None,
        min_score: float = 0.2,
        lost_timeout: float = 2.0,
        send: SendRate | None = None,
    ) -> None:
        """Create an idle lock.

        Args:
            client: Client used to send rotation commands, unless ``send`` is given.
            hfov_deg: Horizontal field of view at 1x zoom (A8 Mini spec: about 81).
            model: :class:`PointLock` motion model, ``local`` or ``global``.
            gains: Rate-loop gains; defaults to :class:`LockGains`.
            min_score: Scores below this count as lost.
            lost_timeout: Seconds lost before the lock is released.
            send: Coroutine taking (yaw, pitch) speeds, replacing ``client.rotate_nowait``.
        """
        if send is None:
            if client is None:
                raise ValueError("pass a client or a send coroutine")
            send = client.rotate_nowait
        self._send = send
        self.hfov_deg = hfov_deg
        self.model: PointModel = model
        self.controller = RateController(gains)
        self.min_score = min_score
        self.lost_timeout = lost_timeout
        self._tracker: PointLock | None = None
        self._last_time: float | None = None
        self._lost_since: float | None = None
        self.status = LockStatus(LockState.IDLE)

    @property
    def active(self) -> bool:
        """Whether a spot is locked (tracking or searching)."""
        return self._tracker is not None

    def lock(self, frame: NDArray[np.uint8], x: float, y: float, size: int = 60) -> None:
        """Lock onto pixel (x, y) of ``frame``; ``size`` only sets the drawn box."""
        tracker = PointLock(self.model)
        tracker.init(frame, (round(x - size / 2), round(y - size / 2), size, size))
        self._tracker = tracker
        self.controller.reset()
        self._last_time = None
        self._lost_since = None
        height, width = frame.shape[:2]
        self.status = LockStatus(LockState.LOCKED, x, y, width, height, 1.0)

    async def update(
        self, frame: NDArray[np.uint8], *, zoom: float = 1.0, timestamp: float | None = None
    ) -> LockStatus:
        """Track into ``frame``, send one rotation command, and return the new status."""
        tracker = self._tracker
        if tracker is None:
            return self.status
        now = time.monotonic() if timestamp is None else timestamp
        dt = 0.0 if self._last_time is None else now - self._last_time
        self._last_time = now
        # Optical flow takes a few milliseconds; keep it off the event loop.
        await asyncio.to_thread(tracker.update, frame)
        if self._tracker is not tracker:  # released or re-locked meanwhile
            return self.status
        height, width = frame.shape[:2]
        x, y = tracker.center()
        score = tracker.getTrackingScore()
        error = pixel_error_deg(x, y, width, height, hfov_deg=self.hfov_deg, zoom=zoom)
        on_screen = 0 <= x < width and 0 <= y < height
        if score >= self.min_score:
            self._lost_since = None
            state = LockState.LOCKED
            command = self.controller.update(*error, dt)
        else:
            self._lost_since = self._lost_since if self._lost_since is not None else now
            if now - self._lost_since > self.lost_timeout:
                await self.release()
                return self.status
            state = LockState.SEARCHING
            self.controller.reset()
            command = (0, 0)
        await self._send(*command)
        self.status = LockStatus(state, x, y, width, height, score, error, command, on_screen)
        return self.status

    async def release(self) -> None:
        """Stop tracking and stop the gimbal."""
        was_active = self._tracker is not None
        self._tracker = None
        self.controller.reset()
        self.status = LockStatus(LockState.IDLE)
        if was_active:
            await self._send(0, 0)
