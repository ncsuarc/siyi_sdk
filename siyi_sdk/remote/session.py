# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Everything the camera service does with the camera, independent of the network.

:class:`CameraSession` owns one :class:`SIYIClient`, the video used for tracking,
the attitude history, the point lock and the speed-setpoint deadman. It reports
what happens through ``emit(event, params)`` callbacks; :mod:`siyi_sdk.remote.server`
puts a websocket in front of it, and tests drive it directly.
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import structlog

from siyi_sdk.client import SIYIClient
from siyi_sdk.exceptions import ConfigurationError
from siyi_sdk.models import (
    DataStreamFreq,
    FirmwareVersion,
    FunctionFeedback,
    GimbalAttitude,
    GimbalDataType,
)
from siyi_sdk.remote import protocol
from siyi_sdk.remote.lock_config import CONTROLLERS, LockConfig, Mount, mounted_inverted
from siyi_sdk.remote.protocol import ConflictError, ServiceUnavailableError
from siyi_sdk.tracking.attitude import AttitudeHistory
from siyi_sdk.tracking.control import LoopModel, TurnRateEstimator
from siyi_sdk.transport.base import Unsubscribe
from siyi_sdk.transport.udp import UDPTransport

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from siyi_sdk.stream import SIYIStream, StreamFrame
    from siyi_sdk.tracking.firmware import FirmwarePointLock
    from siyi_sdk.tracking.firmware_link import FirmwareLink
    from siyi_sdk.tracking.gimbal import GimbalPointLock

logger: Final = structlog.get_logger(__name__)

EmitCallback = Callable[[str, dict[str, Any]], None]
VideoMode = Literal["auto", "rtsp", "firmware", "both", "none"]

# Firmware whose private tracking channel was inspected (docs/firmware-tracking.md).
_FIRMWARE_TRACKING: Final = ((0, 3, 7), (0, 4, 9))
# Attitude older than this means the camera is not answering.
_ATTITUDE_FRESH_S: Final = 1.0
_VIDEO_FRESH_S: Final = 1.0
_MONITOR_PERIOD_S: Final = 0.5
_MOTION_PERIOD_S: Final = 0.02  # 50 Hz
# Resend an unchanged speed this often: 0x07 holds until the next one, but UDP may drop it.
_MOTION_REFRESH_S: Final = 0.1
_LOCK_EVENT_PERIOD_S: Final = 0.1  # lock_status at 10 Hz, plus every state change


class CameraSession:
    """Camera, video, telemetry and point lock for one A8 Mini.

    Args:
        ip: Camera address.
        config: Point-lock settings; loaded from ``config_path`` when omitted.
        config_path: Where lock settings and calibrations are saved (None: don't save).
        client: Use this client instead of opening a UDP one (it is not closed).
        video: Tracking video source. ``auto`` uses the camera's firmware link when
            the firmware supports it and RTSP otherwise; ``both`` decodes both and
            prefers the link, like the debugging dashboard; ``none`` disables video
            (no point lock).
        setpoint_timeout: Stop a speed setpoint this long after its last update.
        attitude_hz: Rate of ``attitude`` events (the camera streams at 50 Hz).
        frame_buffer_s: How much recent video to keep for locking on a clicked frame.
    """

    def __init__(
        self,
        ip: str = "192.168.144.25",
        *,
        config: LockConfig | None = None,
        config_path: Path | str | None = None,
        client: SIYIClient | None = None,
        video: VideoMode = "auto",
        setpoint_timeout: float = 0.3,
        attitude_hz: float = 20.0,
        frame_buffer_s: float = 1.0,
    ) -> None:
        """Configure; nothing talks to the camera until :meth:`start`."""
        self.ip = ip
        self.config_path = config_path
        self.config = config if config is not None else LockConfig.load(config_path)
        self._owns_client = client is None
        self.client = client or SIYIClient(UDPTransport(ip=ip), max_retries=2, auto_reconnect=True)
        self.video_mode: VideoMode = video
        self.setpoint_timeout = setpoint_timeout
        self.attitude_period = 1.0 / attitude_hz if attitude_hz > 0 else float("inf")
        self.frame_buffer_s = frame_buffer_s

        self._listeners: list[EmitCallback] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._unsubscribe: list[Unsubscribe] = []
        self._started = False

        # Camera state
        self.firmware_version: FirmwareVersion | None = None
        self.attitude: GimbalAttitude | None = None
        self.attitude_time = 0.0
        self.history = AttitudeHistory()
        self.zoom: float | None = None
        self.zoom_max: float | None = None
        self._stream_requested = False
        self._last_attitude_event = 0.0
        self._link_state: dict[str, Any] | None = None

        # Video
        self.stream: SIYIStream | None = None
        self.firmware_link: FirmwareLink | None = None
        self._frames: deque[tuple[float, NDArray[np.uint8]]] = deque()
        self.frame_time = 0.0
        self.video_source: str | None = None

        # Speed setpoints: kind -> (value, deadline, last_sent)
        self._setpoints: dict[str, tuple[Any, float, float]] = {}

        # Point lock
        self.lock: GimbalPointLock | FirmwarePointLock | None = None
        self.lock_reason: str | None = None
        self.lock_exit_confirmed = True
        self.lock_ms: float | None = None
        self._lock_guard = asyncio.Lock()
        self._lock_last_rate: tuple[int, int] | None = None
        self._last_lock_event = 0.0
        self._last_lock_state: str | None = None
        self._turn_rate: tuple[tuple[Any, ...], TurnRateEstimator] | None = None
        self.calibrating = False
        self._recorder: Any = None  # FrameMotionRecorder, created when video starts

    # ======================================================================
    # Lifecycle and events
    # ======================================================================

    def subscribe(self, callback: EmitCallback) -> Unsubscribe:
        """Receive ``(event, params)`` for every event; callbacks must not block."""
        self._listeners.append(callback)

        def unsubscribe() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return unsubscribe

    def _emit(self, event: str, params: dict[str, Any]) -> None:
        for callback in tuple(self._listeners):
            try:
                callback(event, params)
            except Exception as exc:
                logger.error("event_listener_failed", event_name=event, error=str(exc))

    async def start(self) -> None:
        """Start talking to the camera; keeps retrying in the background until it answers."""
        if self._started:
            return
        self._started = True
        self._unsubscribe.append(self.client.on_attitude(self._on_attitude))
        self._unsubscribe.append(self.client.on_function_feedback(self._on_feedback))
        self._tasks = [
            asyncio.create_task(self._monitor(), name="siyi-session-monitor"),
            asyncio.create_task(self._motion_loop(), name="siyi-session-motion"),
        ]

    async def close(self) -> None:
        """Stop motion, release any lock, stop video and close the camera link."""
        if not self._started:
            return
        self._started = False
        with contextlib.suppress(Exception):
            await self.stop_motion()
        with contextlib.suppress(Exception):
            await self.release_lock("Camera service stopping")
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        await self._stop_video()
        if self._owns_client:
            await self.client.close()

    async def __aenter__(self) -> CameraSession:  # noqa: D105
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:  # noqa: D105
        await self.close()

    # ======================================================================
    # Camera link
    # ======================================================================

    @property
    def camera_alive(self) -> bool:
        """Whether attitude telemetry shows the camera is answering."""
        return time.monotonic() - self.attitude_time < _ATTITUDE_FRESH_S

    @property
    def video_live(self) -> bool:
        """Whether tracking video arrived recently."""
        return time.monotonic() - self.frame_time < _VIDEO_FRESH_S

    def firmware_tracking_supported(self) -> bool:
        """Whether this camera's firmware has the inspected private tracking channel."""
        version = self.firmware_version
        if version is None:
            return False
        camera = FirmwareVersion.decode_word(version.camera)
        gimbal = FirmwareVersion.decode_word(version.gimbal)
        return (camera, gimbal) == _FIRMWARE_TRACKING

    async def _monitor(self) -> None:
        """Connect, keep the attitude stream and video running, and report link changes."""
        while True:
            try:
                await self._ensure_connected()
                if self.client.connection_event.is_set():
                    await self._ensure_telemetry()
                    await self._ensure_video()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("session_monitor_retry", error=str(exc))
            self._report_link()
            await asyncio.sleep(_MONITOR_PERIOD_S)

    async def _ensure_connected(self) -> None:
        if self.client.connection_event.is_set():
            return
        # The client reconnects by itself for a while; once it has given up (or before
        # the first connect), connecting again raises nothing but may still fail.
        with contextlib.suppress(Exception):
            await self.client.connect()

    async def _ensure_telemetry(self) -> None:
        if self.firmware_version is None:
            self.firmware_version = await self.client.get_firmware_version()
            self.zoom_max = None
        stale = time.monotonic() - self.attitude_time > 2.0
        if not self._stream_requested or stale:
            await self.client.request_gimbal_stream(GimbalDataType.ATTITUDE, DataStreamFreq.HZ50)
            self._stream_requested = True
        if self.zoom is None:
            await self.refresh_zoom()

    def _report_link(self) -> None:
        state = {
            "camera": self.camera_alive,
            "video": self.video_live,
            "source": self.video_source if self.video_live else None,
            "firmware_tracking": self.firmware_tracking_supported(),
        }
        if state != self._link_state:
            if self._link_state is not None and self._link_state["camera"] and not state["camera"]:
                # Telemetry stopped: the stream is requested again when it comes back.
                self._stream_requested = False
            self._link_state = state
            self._emit(protocol.EVENT_LINK, state)

    def _on_attitude(self, att: GimbalAttitude) -> None:
        now = time.monotonic()
        self.attitude = att
        self.attitude_time = now
        self.history.add(now, att.yaw_deg, att.pitch_deg)
        if now - self._last_attitude_event >= self.attitude_period:
            self._last_attitude_event = now
            self._emit(protocol.EVENT_ATTITUDE, {"attitude": att, "t": now})

    def _on_feedback(self, feedback: FunctionFeedback) -> None:
        self._emit(protocol.EVENT_FEEDBACK, {"feedback": feedback, "t": time.monotonic()})

    async def refresh_zoom(self) -> float | None:
        """Read the zoom level (and range once); pixel-to-angle math depends on it."""
        if self.zoom_max is None:
            self.zoom_max = (await self.client.get_zoom_range()).max_zoom
        zoom = await self.client.get_current_zoom()
        if zoom != self.zoom:
            self.zoom = zoom
            self._emit(protocol.EVENT_ZOOM, {"zoom": zoom, "max": self.zoom_max})
        return self.zoom

    @property
    def mount(self) -> Mount:
        """How the gimbal is mounted, read from the live attitude."""
        if self.attitude is not None and mounted_inverted(self.attitude.pitch_deg):
            return "inverted"
        return "normal"

    # ======================================================================
    # Video
    # ======================================================================

    def _wants(self) -> tuple[bool, bool]:
        """(RTSP, firmware link) wanted for the configured video mode."""
        mode = self.video_mode
        supported = self.firmware_tracking_supported()
        if mode == "none":
            return False, False
        if mode == "rtsp":
            return True, False
        if mode == "firmware":
            return False, supported
        if mode == "both":
            return True, supported
        # auto: wait for the firmware version before deciding.
        if self.firmware_version is None:
            return False, False
        return not supported, supported

    async def _ensure_video(self) -> None:
        want_rtsp, want_link = self._wants()
        if (want_rtsp or want_link) and self._recorder is None:
            from siyi_sdk.tracking.calibrate import FrameMotionRecorder

            self._recorder = FrameMotionRecorder()
        if want_link and self.firmware_link is None:
            from siyi_sdk.tracking.firmware_link import FirmwareLink

            self.firmware_link = FirmwareLink(
                self.ip, on_frame=self._on_link_frame, on_failure=self._on_link_failure
            )
            self.firmware_link.start()
        if want_rtsp and self.stream is None:
            stream = self.client.create_stream()
            stream.on_frame(self._on_rtsp_frame)
            self.stream = stream
            try:
                await stream.start()
            except Exception:
                self.stream = None
                with contextlib.suppress(Exception):
                    await stream.stop()
                raise

    async def _stop_video(self) -> None:
        link, self.firmware_link = self.firmware_link, None
        if link is not None:
            with contextlib.suppress(Exception):
                await link.stop()
        stream, self.stream = self.stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.stop()
        self._frames.clear()

    async def _on_link_frame(self, image: NDArray[np.uint8], timestamp: float) -> None:
        await self.on_frame(image, timestamp, source="firmware")

    async def _on_link_failure(self, reason: str) -> None:
        await self.release_lock(reason)

    async def _on_rtsp_frame(self, frame: StreamFrame) -> None:
        link = self.firmware_link
        if link is not None and link.ready:
            return  # mixing two pictures with different delays and sizes confuses the tracker
        import cv2

        # Grayscale is all the tracker needs, and a third of the memory in the frame buffer.
        gray: NDArray[np.uint8] = cv2.cvtColor(frame.frame, cv2.COLOR_BGR2GRAY)  # type: ignore[assignment]
        await self.on_frame(gray, frame.timestamp, source="rtsp")

    async def on_frame(
        self, image: NDArray[np.uint8], timestamp: float, *, source: str = "external"
    ) -> None:
        """Feed one decoded frame (``timestamp`` on this computer's monotonic clock).

        Video sources call this; tests and custom pipelines may call it directly.
        """
        self.frame_time = time.monotonic()
        self.video_source = source
        frames = self._frames
        frames.append((timestamp, image))
        while frames and timestamp - frames[0][0] > self.frame_buffer_s:
            frames.popleft()
        recorder = self._recorder
        if self.calibrating and recorder is not None and recorder.recording:
            await recorder.add_async(image, timestamp)
        lock = self.lock
        if lock is None or not lock.active:
            return
        started = time.perf_counter()
        try:
            status = await lock.update(image, zoom=self.zoom or 1.0, timestamp=timestamp)
        except Exception as exc:
            logger.warning("point_lock_update_failed", error=str(exc))
            await self.release_lock("Point lock stopped after an error")
            return
        self.lock_ms = round((time.perf_counter() - started) * 1000, 1)
        if status.state.value == "idle" and self.lock is lock:
            self.lock_reason = getattr(lock, "reason", None) or "Tracking lost"
            if hasattr(lock, "exit_confirmed"):
                self.lock_exit_confirmed = lock.exit_confirmed
        self._report_lock()

    def frame_at(self, t: float | None) -> tuple[float, NDArray[np.uint8]] | None:
        """The buffered frame nearest time ``t`` (newest when None)."""
        frames = self._frames
        if not frames:
            return None
        if t is None:
            return frames[-1]
        times = [stamp for stamp, _ in frames]
        i = bisect.bisect_left(times, t)
        candidates = [j for j in (i - 1, i) if 0 <= j < len(frames)]
        best = min(candidates, key=lambda j: abs(times[j] - t))
        return frames[best]

    # ======================================================================
    # Speed setpoints (deadman)
    # ======================================================================

    async def set_rate(self, yaw: float, pitch: float) -> None:
        """Turn at (yaw, pitch) 0x07 speeds until updated, or until updates stop.

        Values are clamped to -100..100. Manual motion releases a point lock.
        """
        value = (_clamp_speed(yaw), _clamp_speed(pitch))
        await self._set_setpoint("rate", value, value == (0, 0))

    async def set_zoom_rate(self, direction: float) -> None:
        """Zoom in (1), out (-1) or stop (0) until updated, or until updates stop."""
        value = 0 if direction == 0 else (1 if direction > 0 else -1)
        await self._set_setpoint("zoom", value, value == 0)

    async def _set_setpoint(self, kind: str, value: Any, stopping: bool) -> None:  # noqa: ANN401
        now = time.monotonic()
        if stopping:
            if self._setpoints.pop(kind, None) is not None:
                await self._send_stop(kind)
            return
        if kind == "rate" and self.lock is not None and self.lock.active:
            await self.release_lock("Point lock released by manual control")
        previous = self._setpoints.get(kind)
        self._setpoints[kind] = (value, now + self.setpoint_timeout, now)
        if previous is None or previous[0] != value:
            await self._send_setpoint(kind, value)

    async def stop_motion(self) -> None:
        """Stop every speed setpoint now."""
        kinds, self._setpoints = list(self._setpoints), {}
        for kind in kinds:
            await self._send_stop(kind)

    async def _send_setpoint(self, kind: str, value: Any) -> None:  # noqa: ANN401
        try:
            if kind == "rate":
                await self.client.rotate_nowait(*value)
            else:
                await self.client.manual_zoom_nowait(value)
        except Exception as exc:
            logger.debug("setpoint_send_failed", kind=kind, error=str(exc))

    async def _send_stop(self, kind: str) -> None:
        # UDP delivery is unconfirmed: send three stops without waiting for ACKs.
        for _ in range(3):
            await self._send_setpoint(kind, (0, 0) if kind == "rate" else 0)
        if kind == "zoom":
            with contextlib.suppress(Exception):
                await asyncio.sleep(0.15)
                await self.refresh_zoom()

    async def _motion_loop(self) -> None:
        while True:
            await asyncio.sleep(_MOTION_PERIOD_S)
            now = time.monotonic()
            for kind, (value, deadline, last_sent) in list(self._setpoints.items()):
                if now >= deadline:
                    self._setpoints.pop(kind, None)
                    logger.info("setpoint_timed_out", kind=kind)
                    await self._send_stop(kind)
                elif now - last_sent >= _MOTION_REFRESH_S:
                    self._setpoints[kind] = (value, deadline, now)
                    await self._send_setpoint(kind, value)

    # ======================================================================
    # Point lock
    # ======================================================================

    async def start_lock(
        self,
        x: float,
        y: float,
        *,
        frame_time: float | None = None,
        controller: str | None = None,
        size: int | None = None,
    ) -> dict[str, Any]:
        """Lock the spot at normalized offset (x, y) from the image centre.

        ``x`` and ``y`` are -0.5..0.5 (right and down positive). ``frame_time`` is
        when the clicked frame arrived here (this computer's monotonic clock); the
        nearest buffered frame is used, the newest when omitted.

        Raises:
            ConfigurationError: Bad coordinates or controller.
            ServiceUnavailableError: No camera or no video.
            ConflictError: A loop measurement is running, or the mounting changed.
        """
        if not (-0.5 <= x <= 0.5 and -0.5 <= y <= 0.5):
            raise ConfigurationError("x and y must be between -0.5 and 0.5")
        control = controller or self.config.control
        if control not in CONTROLLERS:
            raise ConfigurationError(f"controller must be one of {', '.join(CONTROLLERS)}")
        async with self._lock_guard:
            await self._release_lock("Point lock replaced")
            if self.calibrating:
                raise ConflictError("Wait for the loop measurement to finish")
            if not self.camera_alive or not self.video_live:
                raise ServiceUnavailableError("Point lock needs live video from a connected camera")
            if frame_time is not None:
                frame_time -= self.config.ground_extra_video_delay_ms / 1000
            found = self.frame_at(frame_time)
            if found is None:
                raise ServiceUnavailableError("No video frame to lock on yet")
            stamp, image = found
            height, width = image.shape[:2]
            px, py = (x + 0.5) * width, (y + 0.5) * height
            box = size or max(40, width // 20)
            # Stop manual motion so it doesn't fight the lock.
            self._setpoints.pop("rate", None)
            if control == "firmware":
                await self._start_firmware_lock(image, px, py, box, stamp)
            else:
                await self._start_app_lock(image, px, py, box, control)
            self._last_lock_state = None
            self._report_lock(force=True)
            return self.lock_snapshot()

    async def _start_app_lock(
        self, image: NDArray[np.uint8], px: float, py: float, box: int, control: str
    ) -> None:
        config = self.config
        config.apply_mount(self.mount)
        # Pixel errors become angles through the zoom; make sure it isn't stale.
        with contextlib.suppress(Exception):
            await self.refresh_zoom()
        lock = config.build_app_lock(
            send_rate=self._send_lock_rate,
            send_angle=self._send_lock_angle,
            attitude=self.history,
            control=control,  # type: ignore[arg-type]
            inverted=self.mount == "inverted",
            turn_rate=self._turn_rate_for,
        )
        await asyncio.to_thread(lock.lock, image, px, py, box)
        self.lock = lock
        self.lock_reason = None
        self.lock_exit_confirmed = True
        self._lock_last_rate = None  # always send the first command of a new lock

    async def _start_firmware_lock(
        self, image: NDArray[np.uint8], px: float, py: float, box: int, stamp: float
    ) -> None:
        if not self.firmware_tracking_supported():
            raise ServiceUnavailableError(
                "Firmware tracking needs the inspected camera 0.3.7 / gimbal 0.4.9 firmware"
            )
        link = self.firmware_link
        if link is not None and not link.ready:
            raise ServiceUnavailableError(
                link.error
                or "Waiting for the camera's tracking video (it starts at a keyframe, "
                "about every 2.5 s)"
            )
        await self._emergency_stop()
        if link is not None and link.client is not None:
            client, owns = link.client, False
        else:
            from siyi_sdk.firmware_tracking import FirmwareTrackingClient

            client, owns = FirmwareTrackingClient(self.ip), True
        lock = self.config.build_firmware_lock(
            client, owns_connection=owns, stop=self._emergency_stop
        )
        self.lock = lock
        self.lock_reason = None
        try:
            await lock.start(image, px, py, size=box, timestamp=stamp, zoom=self.zoom or 1.0)
        except Exception as exc:
            self.lock = None
            self.lock_reason = (
                lock.reason or f"Firmware tracking unavailable: {exc or type(exc).__name__}"
            )
            self.lock_exit_confirmed = lock.exit_confirmed
            raise ServiceUnavailableError(self.lock_reason) from exc

    async def release_lock(self, reason: str | None = None) -> dict[str, Any]:
        """Stop the point lock (no-op without one); returns the final lock status."""
        async with self._lock_guard:
            await self._release_lock(reason)
        self._report_lock(force=True)
        return self.lock_snapshot()

    async def _release_lock(self, reason: str | None) -> None:
        lock, self.lock = self.lock, None
        if lock is None:
            return
        from siyi_sdk.tracking.firmware import FirmwarePointLock

        if isinstance(lock, FirmwarePointLock):
            await lock.release(reason or "Point lock released")
            self.lock_reason = lock.reason
            self.lock_exit_confirmed = lock.exit_confirmed
        else:
            if lock.active:
                await lock.release()
            self.lock_reason = reason or self.lock_reason
        if reason:
            logger.info("point_lock_released", reason=reason)

    async def _send_lock_rate(self, yaw: int, pitch: int) -> None:
        # The lock sends every frame; a stop goes out (as three packets) only once.
        if (yaw, pitch) == (0, 0) and self._lock_last_rate == (0, 0):
            return
        self._lock_last_rate = (yaw, pitch)
        for _ in range(1 if (yaw, pitch) != (0, 0) else 3):
            with contextlib.suppress(Exception):
                await self.client.rotate_nowait(yaw, pitch)

    async def _send_lock_angle(self, yaw: float, pitch: float) -> None:
        with contextlib.suppress(Exception):
            await self.client.set_attitude_nowait(yaw, pitch)

    async def _emergency_stop(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.client.rotate_nowait(0, 0), timeout=0.4)

    def _turn_rate_for(self, loop: LoopModel) -> TurnRateEstimator | None:
        """The turn-rate estimate earlier locks learned, while the calibration is unchanged."""
        key = (loop.deg_per_unit, loop.dead_time_s, loop.motor_tau_s)
        if self._turn_rate is None or self._turn_rate[0] != key:
            self._turn_rate = (key, TurnRateEstimator(loop.dead_time_s, loop.motor_tau_s))
        return self._turn_rate[1]

    def lock_snapshot(self) -> dict[str, Any]:
        """The lock's current state as plain values."""
        lock = self.lock
        if lock is None or not lock.active:
            snapshot: dict[str, Any] = {"state": "idle"}
            if self.lock_reason or not self.lock_exit_confirmed:
                snapshot.update(reason=self.lock_reason, exit_confirmed=self.lock_exit_confirmed)
            return snapshot
        s = lock.status
        snapshot = {
            "state": s.state.value,
            "controller": lock.control,
            "score": round(s.score, 2),
            "on_screen": s.on_screen,
            "x": round(s.x / s.width - 0.5, 4) if s.width else 0.0,
            "y": round(s.y / s.height - 0.5, 4) if s.height else 0.0,
            "update_ms": self.lock_ms,
        }
        if lock.control != "firmware":
            snapshot.update(
                error_deg=[round(v, 2) for v in s.error_deg],
                command=list(s.command),
                compensated=s.compensated,
                target_rate_deg_s=[round(v, 1) for v in s.target_rate_deg_s],
                turn_scale=[round(v, 3) for v in s.turn_scale],
                gain_scale=round(s.gain_scale, 3),
            )
        return snapshot

    def _report_lock(self, *, force: bool = False) -> None:
        now = time.monotonic()
        snapshot = self.lock_snapshot()
        changed = snapshot["state"] != self._last_lock_state
        if not (force or changed or now - self._last_lock_event >= _LOCK_EVENT_PERIOD_S):
            return
        self._last_lock_event = now
        self._last_lock_state = snapshot["state"]
        self._emit(protocol.EVENT_LOCK, {**snapshot, "t": now})

    # ======================================================================
    # Calibration and settings
    # ======================================================================

    async def calibrate(self) -> dict[str, Any]:
        """Measure turn rate, delays, axis directions and field of view, and store them.

        Turns the gimbal about 20 degrees on each axis and back. Point the camera
        at a textured, still scene first.
        """
        from siyi_sdk.tracking.calibrate import CalibrationError, calibrate_loop

        if self.calibrating:
            raise ConflictError("A measurement is already running")
        if not self.camera_alive or not self.video_live or self._recorder is None:
            raise ServiceUnavailableError("Measuring needs live video from a connected camera")
        await self.release_lock("Point lock released for loop measurement")
        await self.stop_motion()
        # The field-of-view fit divides by the zoom; a stale zoom gives a wrong lens angle.
        await self.refresh_zoom()
        self.calibrating = True
        try:
            result = await calibrate_loop(
                self.client.rotate_nowait,
                self.history,
                self._recorder,
                hfov_deg=self.config.hfov_deg,
                zoom=self.zoom or 1.0,
            )
        except CalibrationError as exc:
            raise protocol.OperationFailedError(str(exc)) from exc
        finally:
            self.calibrating = False
            self._recorder.stop()
            await self._send_stop("rate")
        notes = self.config.apply_calibration(result, self.mount)
        self._turn_rate = None
        self._save_config()
        return {
            "config": self.config.to_dict(),
            "notes": notes,
            "fit_error_deg": round(result.yaw.fit_error_deg, 2),
        }

    def learned_loop(self) -> dict[str, Any]:
        """What locks have learned about the turn rate and command delay."""
        config = self.config
        estimator = self._turn_rate[1] if self._turn_rate else None
        if estimator is None or min(estimator.samples) < 50:
            return {"ready": False, "samples": list(estimator.samples) if estimator else [0, 0]}
        return {
            "ready": True,
            "samples": list(estimator.samples),
            "turn_scale": [round(v, 3) for v in estimator.scale],
            "deg_per_unit": [
                round(config.deg_per_unit_yaw * estimator.scale[0], 4),
                round(config.deg_per_unit_pitch * estimator.scale[1], 4),
            ],
            "command_delay_ms": round(
                (estimator.dead_time_s + config.motor_tau_ms / 1000) * 1000, 1
            ),
        }

    async def apply_learned_loop(self) -> dict[str, Any]:
        """Fold the learned turn rate and command delay into the stored calibration."""
        async with self._lock_guard:
            learned = self.learned_loop()
            if not learned["ready"]:
                raise ConflictError(
                    "Not enough learned yet: lock onto a moving spot (or pan while locked) so "
                    "the gimbal turns on both axes for a while. "
                    f"Windows so far: {learned['samples']}."
                )
            config = self.config
            config.deg_per_unit_yaw, config.deg_per_unit_pitch = learned["deg_per_unit"]
            config.command_delay_ms = learned["command_delay_ms"]
            if config.mount_profiles:
                config.store_mount(self.mount)
            self._turn_rate = None
            self._save_config()
        return {"config": config.to_dict(), "applied": learned}

    def set_lock_config(self, values: dict[str, Any]) -> dict[str, Any]:
        """Change stored lock settings; running locks keep what they started with."""
        self.config.update(values)
        self._save_config()
        return self.config.to_dict()

    def _save_config(self) -> None:
        if self.config_path is not None:
            self.config.save(self.config_path)

    # ======================================================================
    # State
    # ======================================================================

    def state(self) -> dict[str, Any]:
        """Everything a ground station shows at a glance."""
        now = time.monotonic()
        return {
            "t": now,
            "camera": {
                "ip": self.ip,
                "alive": self.camera_alive,
                "firmware": self.firmware_version,
                "firmware_tracking": self.firmware_tracking_supported(),
                "attitude": self.attitude,
                "attitude_age_ms": (
                    round((now - self.attitude_time) * 1000) if self.attitude else None
                ),
                "mount": self.mount,
                "zoom": self.zoom,
                "zoom_max": self.zoom_max,
            },
            "video": {
                "mode": self.video_mode,
                "live": self.video_live,
                "source": self.video_source if self.video_live else None,
                "buffered_frames": len(self._frames),
            },
            "motion": {kind: value for kind, (value, _, _) in self._setpoints.items()},
            "lock": self.lock_snapshot(),
            "calibrating": self.calibrating,
        }


def _clamp_speed(value: float) -> int:
    if value != value:  # NaN
        return 0
    return int(max(-100, min(100, round(value))))
