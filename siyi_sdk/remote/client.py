# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Ground-station client for the camera service.

:class:`RemoteCamera` has the same method names, arguments, return types and
exceptions as :class:`SIYIClient` for the commands the service passes through,
plus speed setpoints that stop by themselves and point lock running on the
aircraft::

    async with RemoteCamera("ws://192.168.144.30:8765") as camera:
        camera.on_attitude(lambda att: print(att.yaw_deg))
        await camera.set_attitude(10.0, -30.0)
        camera.rate(40, 0)           # keeps turning while you keep calling it
        await camera.lock(0.1, -0.05, frame_time=frame.timestamp)
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import time
from collections.abc import Callable
from typing import Any, Final, Literal

import structlog
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from siyi_sdk.exceptions import ConnectionError, NotConnectedError
from siyi_sdk.exceptions import TimeoutError as SIYITimeoutError
from siyi_sdk.models import (
    AircraftAttitude,
    CameraSystemInfo,
    CaptureFuncType,
    CenteringAction,
    EncodingParams,
    FirmwareVersion,
    FunctionFeedback,
    GimbalAttitude,
    GimbalMotionMode,
    HardwareID,
    MagneticEncoderAngles,
    RawGPS,
    SetAttitudeAck,
    StreamType,
    ZoomRange,
)
from siyi_sdk.remote import protocol
from siyi_sdk.transport.base import Unsubscribe

logger: Final = structlog.get_logger(__name__)

Controller = Literal["angle", "rate", "firmware"]

# Resend held setpoints this often; the service stops them after 0.3 s of silence.
_SETPOINT_PERIOD_S: Final = 0.1
_PING_PERIOD_S: Final = 2.0
_CLOCK_SAMPLES: Final = 8


class RemoteCamera:
    """Control an A8 Mini through the camera service on the aircraft.

    Args:
        url: Service address, e.g. ``ws://192.168.144.30:8765``.
        name: Shown to other clients when this one has control.
        timeout: Default reply timeout in seconds.
        take_control: Ask for control on every (re)connect if nobody else has it.
        reconnect: Reconnect after the link drops.
        ping_interval: Websocket keepalive; a dead link is noticed within
            ``ping_interval + ping_timeout``.
        ping_timeout: Keepalive reply timeout.
    """

    def __init__(
        self,
        url: str = "ws://localhost:8765",
        *,
        name: str = "",
        timeout: float = 5.0,
        take_control: bool = True,
        reconnect: bool = True,
        ping_interval: float = 1.0,
        ping_timeout: float = 3.0,
    ) -> None:
        """Configure; nothing connects until :meth:`connect`."""
        self.url = url
        self.name = name
        self.timeout = timeout
        self.auto_take_control = take_control
        self.reconnect = reconnect
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout

        self.connection_event = asyncio.Event()
        self.is_controller = False
        self.control_holder: str | None = None
        self.server_info: dict[str, Any] = {}

        self._connection: ClientConnection | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._listeners: dict[str, list[Callable[[Any], None]]] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._session_tasks: list[asyncio.Task[None]] = []
        self._closing = False
        self._rate: tuple[int, int] = (0, 0)
        self._zoom: int = 0
        self._clock: list[tuple[float, float]] = []  # (rtt, offset)
        self.last_link: dict[str, Any] | None = None

    # ======================================================================
    # Connection
    # ======================================================================

    async def connect(self) -> None:
        """Connect (raising on failure) and keep the connection up in the background."""
        self._closing = False
        await self._open()
        self._tasks.append(asyncio.create_task(self._supervise(), name="remote-camera-supervisor"))

    async def close(self) -> None:
        """Stop held setpoints and disconnect."""
        self._closing = True
        if self.is_controller and self._connection is not None:
            with contextlib.suppress(Exception):
                await self.stop()
        for task in (*self._tasks, *self._session_tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, *self._session_tasks, return_exceptions=True)
        self._tasks.clear()
        self._session_tasks.clear()
        connection, self._connection = self._connection, None
        if connection is not None:
            await connection.close()
        self._fail_pending(ConnectionError("Connection closed"))
        self.connection_event.clear()

    async def __aenter__(self) -> RemoteCamera:  # noqa: D105
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:  # noqa: D105
        await self.close()

    async def _open(self) -> None:
        try:
            connection = await connect(
                self.url, ping_interval=self.ping_interval, ping_timeout=self.ping_timeout
            )
        except OSError as exc:
            raise ConnectionError(f"Cannot reach camera service at {self.url}: {exc}") from exc
        self._connection = connection
        self._session_tasks = [
            asyncio.create_task(self._read(connection), name="remote-camera-reader"),
            asyncio.create_task(self._hold_setpoints(), name="remote-camera-setpoints"),
            asyncio.create_task(self._sync_clock(), name="remote-camera-clock"),
        ]
        self.connection_event.set()
        self.server_info = await self.call("hello", name=self.name)
        if self.server_info.get("protocol") != protocol.PROTOCOL_VERSION:
            logger.warning("protocol_mismatch", server=self.server_info.get("protocol"))
        if self.auto_take_control and not self.server_info.get("controller"):
            with contextlib.suppress(protocol.NotControllerError):
                await self.take_control()

    async def _supervise(self) -> None:
        delay = 0.5
        while True:
            connection = self._connection
            if connection is not None:
                await connection.wait_closed()
            if self._closing:
                return
            self._connection_lost()
            if not self.reconnect:
                return
            while not self._closing:
                await asyncio.sleep(delay)
                try:
                    await self._open()
                    delay = 0.5
                    break
                except Exception as exc:
                    logger.debug("remote_reconnect_failed", error=str(exc))
                    delay = min(delay * 2, 5.0)

    def _connection_lost(self) -> None:
        self.connection_event.clear()
        for task in self._session_tasks:
            task.cancel()
        self._session_tasks = []
        self._connection = None
        self.is_controller = False
        self._clock.clear()
        self._fail_pending(ConnectionError("Connection to camera service lost"))
        self._dispatch(protocol.EVENT_LINK, {"service": False})

    def _fail_pending(self, exc: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def _read(self, connection: ClientConnection) -> None:
        with contextlib.suppress(ConnectionClosed):
            async for text in connection:
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "id" in msg and msg.get("id") is not None and "method" not in msg:
                    future = self._pending.pop(msg["id"], None)
                    if future is None or future.done():
                        continue
                    if "error" in msg:
                        future.set_exception(protocol.decode_error(msg["error"]))
                    else:
                        future.set_result(protocol.decode_value(msg.get("result")))
                elif isinstance(msg.get("method"), str):
                    self._dispatch(msg["method"], protocol.decode_value(msg.get("params") or {}))

    # ======================================================================
    # Calls
    # ======================================================================

    async def call(
        self,
        method: str,
        *,
        _timeout: float | None = None,
        **params: object,
    ) -> Any:  # noqa: ANN401 - results are any JSON-RPC value
        """Invoke a service method by name and return its (decoded) result.

        Raises:
            NotConnectedError: Not connected to the service.
            siyi_sdk.exceptions.TimeoutError: No reply in time (cmd_id is -1).
            Any SDK or service exception the service raised.
        """
        connection = self._connection
        if connection is None:
            raise NotConnectedError("Not connected to the camera service")
        msg_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        timeout = self.timeout if _timeout is None else _timeout
        try:
            await connection.send(protocol.request(msg_id, method, params))
            return await asyncio.wait_for(future, timeout)
        except ConnectionClosed as exc:
            raise ConnectionError("Connection to camera service lost") from exc
        except asyncio.TimeoutError:
            raise SIYITimeoutError(cmd_id=-1, timeout_s=timeout) from None
        finally:
            self._pending.pop(msg_id, None)

    async def _notify(self, method: str, **params: Any) -> None:  # noqa: ANN401
        connection = self._connection
        if connection is None:
            return
        with contextlib.suppress(ConnectionClosed):
            await connection.send(protocol.notification(method, params))

    # ======================================================================
    # Control
    # ======================================================================

    async def take_control(self, *, force: bool = False) -> None:
        """Become the controlling ground station; ``force`` takes it from another one."""
        await self.call("take_control", force=force)
        self.is_controller = True

    async def release_control(self) -> None:
        """Let another ground station take control."""
        await self.call("release_control")
        self.is_controller = False

    async def get_state(self) -> dict[str, Any]:
        """Camera, video, motion, lock and control state."""
        result: dict[str, Any] = await self.call("get_state")
        return result

    # ======================================================================
    # Speed setpoints
    # ======================================================================

    def rate(self, yaw: float, pitch: float) -> None:
        """Turn at 0x07 speeds (-100..100); (0, 0) stops.

        The speed is resent every 100 ms while it is non-zero. If this program or the
        link stops, the service stops the gimbal within its setpoint timeout.
        Manual motion releases a point lock.
        """
        self._rate = (round(yaw), round(pitch))
        self._spawn(self._notify("rate", yaw=self._rate[0], pitch=self._rate[1]))

    def zoom_rate(self, direction: int) -> None:
        """Zoom in (1), out (-1) or stop (0); held like :meth:`rate`."""
        self._zoom = 0 if direction == 0 else (1 if direction > 0 else -1)
        self._spawn(self._notify("zoom_rate", direction=self._zoom))

    async def stop(self) -> None:
        """Stop turning and zooming now."""
        self._rate, self._zoom = (0, 0), 0
        await self.call("stop")

    async def _hold_setpoints(self) -> None:
        while True:
            await asyncio.sleep(_SETPOINT_PERIOD_S)
            if self._rate != (0, 0):
                await self._notify("rate", yaw=self._rate[0], pitch=self._rate[1])
            if self._zoom != 0:
                await self._notify("zoom_rate", direction=self._zoom)

    def _spawn(self, coro: Any) -> None:  # noqa: ANN401
        task = asyncio.ensure_future(coro)
        self._tasks.append(task)
        task.add_done_callback(lambda t: self._tasks.remove(t) if t in self._tasks else None)

    # ======================================================================
    # Point lock
    # ======================================================================

    async def lock(
        self,
        x: float,
        y: float,
        *,
        frame_time: float | None = None,
        controller: Controller | None = None,
        size: int | None = None,
    ) -> dict[str, Any]:
        """Lock the spot at normalized offset (x, y) from the image centre.

        ``x``/``y`` run from -0.5 (left/top) to 0.5 (right/bottom). Pass the
        ``time.monotonic()`` time at which the clicked frame arrived on this computer
        (``StreamFrame.timestamp``) so the service locks on the same picture.
        ``controller`` overrides the stored default (``angle``, ``rate``, ``firmware``).
        """
        pi_time = self.to_service_time(frame_time) if frame_time is not None else None
        result: dict[str, Any] = await self.call(
            "lock", x=x, y=y, frame_time=pi_time, controller=controller, size=size
        )
        return result

    async def release(self) -> dict[str, Any]:
        """Release the point lock; returns its final status."""
        result: dict[str, Any] = await self.call("release")
        return result

    async def get_lock(self) -> dict[str, Any]:
        """Current point-lock status."""
        result: dict[str, Any] = await self.call("get_lock")
        return result

    async def calibrate(self) -> dict[str, Any]:
        """Measure the loop (turns the gimbal about 20 degrees per axis) and store it."""
        result: dict[str, Any] = await self.call("calibrate", _timeout=60.0)
        return result

    async def get_lock_config(self) -> dict[str, Any]:
        """Stored point-lock settings."""
        result: dict[str, Any] = await self.call("get_lock_config")
        return result

    async def set_lock_config(self, **values: Any) -> dict[str, Any]:  # noqa: ANN401
        """Change stored point-lock settings, e.g. ``control="rate"``."""
        result: dict[str, Any] = await self.call("set_lock_config", **values)
        return result

    # ======================================================================
    # Clock
    # ======================================================================

    async def _sync_clock(self) -> None:
        while True:
            with contextlib.suppress(Exception):
                await self.ping()
            await asyncio.sleep(_PING_PERIOD_S)

    async def ping(self) -> float:
        """Measure the round trip (seconds) and update the clock offset."""
        sent = time.monotonic()
        reply = await self.call("ping")
        received = time.monotonic()
        rtt = received - sent
        offset = float(reply["t"]) - (sent + received) / 2
        self._clock.append((rtt, offset))
        del self._clock[:-_CLOCK_SAMPLES]
        return rtt

    @property
    def clock_offset(self) -> float | None:
        """Service clock minus this computer's ``time.monotonic()``, from the fastest ping."""
        if not self._clock:
            return None
        return min(self._clock)[1]

    @property
    def rtt(self) -> float | None:
        """Fastest recent round trip in seconds."""
        return min(self._clock)[0] if self._clock else None

    def to_service_time(self, t: float) -> float | None:
        """Convert a local ``time.monotonic()`` time to the service's clock."""
        offset = self.clock_offset
        return None if offset is None else t + offset

    # ======================================================================
    # Events
    # ======================================================================

    def _subscribe(self, event: str, callback: Callable[[Any], None]) -> Unsubscribe:
        listeners = self._listeners.setdefault(event, [])
        listeners.append(callback)

        def unsubscribe() -> None:
            if callback in listeners:
                listeners.remove(callback)

        return unsubscribe

    def _dispatch(self, event: str, params: dict[str, Any]) -> None:
        if event == protocol.EVENT_CONTROL:
            self.is_controller = bool(params.get("controller"))
            self.control_holder = params.get("holder")
            value: Any = params
        elif event == protocol.EVENT_ATTITUDE:
            value = params.get("attitude")
        elif event == protocol.EVENT_FEEDBACK:
            value = params.get("feedback")
        else:
            value = params
        if event == protocol.EVENT_LINK:
            self.last_link = params
        for callback in tuple(self._listeners.get(event, ())):
            try:
                callback(value)
            except Exception as exc:
                logger.error("remote_callback_failed", event_name=event, error=str(exc))

    def on_attitude(self, callback: Callable[[GimbalAttitude], None]) -> Unsubscribe:
        """Gimbal attitude, at the service's event rate (20 Hz by default)."""
        return self._subscribe(protocol.EVENT_ATTITUDE, callback)

    def on_function_feedback(self, callback: Callable[[FunctionFeedback], None]) -> Unsubscribe:
        """Photo and recording results."""
        return self._subscribe(protocol.EVENT_FEEDBACK, callback)

    def on_lock_status(self, callback: Callable[[dict[str, Any]], None]) -> Unsubscribe:
        """Point-lock status: 10 Hz while locked, and on every state change."""
        return self._subscribe(protocol.EVENT_LOCK, callback)

    def on_link(self, callback: Callable[[dict[str, Any]], None]) -> Unsubscribe:
        """Camera and video link changes; ``{"service": False}`` when the service is lost."""
        return self._subscribe(protocol.EVENT_LINK, callback)

    def on_zoom(self, callback: Callable[[dict[str, Any]], None]) -> Unsubscribe:
        """Zoom level changes: ``{"zoom": 2.0, "max": 6.0}``."""
        return self._subscribe(protocol.EVENT_ZOOM, callback)

    def on_control(self, callback: Callable[[dict[str, Any]], None]) -> Unsubscribe:
        """Control changes: ``{"controller": bool, "holder": name}``."""
        return self._subscribe(protocol.EVENT_CONTROL, callback)

    # ======================================================================
    # SIYIClient passthrough (same names, arguments and results)
    # ======================================================================

    async def get_firmware_version(self) -> FirmwareVersion:
        """See :meth:`SIYIClient.get_firmware_version`."""
        result: FirmwareVersion = await self.call("get_firmware_version")
        return result

    async def get_hardware_id(self) -> HardwareID:
        """See :meth:`SIYIClient.get_hardware_id`."""
        result: HardwareID = await self.call("get_hardware_id")
        return result

    async def set_utc_time(self, unix_usec: int) -> bool:
        """See :meth:`SIYIClient.set_utc_time`."""
        result: bool = await self.call("set_utc_time", unix_usec=unix_usec)
        return result

    async def soft_reboot(
        self, *, camera: bool = False, gimbal: bool = False, confirm: bool = False
    ) -> tuple[bool, bool]:
        """See :meth:`SIYIClient.soft_reboot`; needs ``confirm=True``."""
        result = await self.call("soft_reboot", camera=camera, gimbal=gimbal, confirm=confirm)
        return (bool(result[0]), bool(result[1]))

    async def one_key_centering(self, action: CenteringAction = CenteringAction.CENTER) -> None:
        """See :meth:`SIYIClient.one_key_centering`; releases a point lock."""
        await self.call("one_key_centering", action=action)

    async def set_attitude(self, yaw_deg: float, pitch_deg: float) -> SetAttitudeAck:
        """See :meth:`SIYIClient.set_attitude`; releases a point lock."""
        result: SetAttitudeAck = await self.call(
            "set_attitude", yaw_deg=yaw_deg, pitch_deg=pitch_deg
        )
        return result

    async def set_attitude_nowait(self, yaw_deg: float, pitch_deg: float) -> None:
        """See :meth:`SIYIClient.set_attitude_nowait` (the service still replies once sent)."""
        await self.call("set_attitude_nowait", yaw_deg=yaw_deg, pitch_deg=pitch_deg)

    async def set_single_axis(
        self, axis: Literal["yaw", "pitch"], angle_deg: float
    ) -> SetAttitudeAck:
        """See :meth:`SIYIClient.set_single_axis`."""
        result: SetAttitudeAck = await self.call("set_single_axis", axis=axis, angle_deg=angle_deg)
        return result

    async def set_single_axis_nowait(self, axis: Literal["yaw", "pitch"], angle_deg: float) -> None:
        """See :meth:`SIYIClient.set_single_axis_nowait`."""
        await self.call("set_single_axis_nowait", axis=axis, angle_deg=angle_deg)

    async def get_gimbal_mode(self) -> GimbalMotionMode:
        """See :meth:`SIYIClient.get_gimbal_mode`."""
        result: GimbalMotionMode = await self.call("get_gimbal_mode")
        return result

    async def get_gimbal_attitude(self) -> GimbalAttitude:
        """See :meth:`SIYIClient.get_gimbal_attitude`."""
        result: GimbalAttitude = await self.call("get_gimbal_attitude")
        return result

    async def get_magnetic_encoder(self) -> MagneticEncoderAngles:
        """See :meth:`SIYIClient.get_magnetic_encoder`."""
        result: MagneticEncoderAngles = await self.call("get_magnetic_encoder")
        return result

    async def absolute_zoom(self, zoom: float) -> None:
        """See :meth:`SIYIClient.absolute_zoom`."""
        await self.call("absolute_zoom", zoom=zoom)

    async def get_zoom_range(self) -> ZoomRange:
        """See :meth:`SIYIClient.get_zoom_range`."""
        result: ZoomRange = await self.call("get_zoom_range")
        return result

    async def get_current_zoom(self) -> float:
        """See :meth:`SIYIClient.get_current_zoom`."""
        result: float = await self.call("get_current_zoom")
        return result

    async def get_camera_system_info(self) -> CameraSystemInfo:
        """See :meth:`SIYIClient.get_camera_system_info`."""
        result: CameraSystemInfo = await self.call("get_camera_system_info")
        return result

    async def capture(self, func: CaptureFuncType) -> None:
        """See :meth:`SIYIClient.capture`; results arrive via :meth:`on_function_feedback`."""
        await self.call("capture", func=func)

    async def get_encoding_params(self, stream: StreamType) -> EncodingParams:
        """See :meth:`SIYIClient.get_encoding_params`."""
        result: EncodingParams = await self.call("get_encoding_params", stream=stream)
        return result

    async def set_encoding_params(self, params: EncodingParams) -> bool:
        """See :meth:`SIYIClient.set_encoding_params`; releases a point lock."""
        result: bool = await self.call("set_encoding_params", params=params, _timeout=15.0)
        return result

    async def format_sd_card(self, *, timeout: float = 60.0, confirm: bool = False) -> bool:
        """See :meth:`SIYIClient.format_sd_card`; needs ``confirm=True``."""
        result: bool = await self.call(
            "format_sd_card", timeout=timeout, confirm=confirm, _timeout=timeout + 5.0
        )
        return result

    async def get_osd_flag(self) -> bool:
        """See :meth:`SIYIClient.get_osd_flag`."""
        result: bool = await self.call("get_osd_flag")
        return result

    async def set_osd_flag(self, on: bool) -> bool:
        """See :meth:`SIYIClient.set_osd_flag`."""
        result: bool = await self.call("set_osd_flag", on=on)
        return result

    async def send_aircraft_attitude(self, att: AircraftAttitude) -> None:
        """See :meth:`SIYIClient.send_aircraft_attitude`."""
        await self.call("send_aircraft_attitude", att=att)

    async def send_raw_gps(self, gps: RawGPS) -> None:
        """See :meth:`SIYIClient.send_raw_gps`."""
        await self.call("send_raw_gps", gps=gps)
