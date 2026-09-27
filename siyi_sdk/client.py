# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""High-level async client for the SIYI SDK.

This module provides the SIYIClient class, which manages the full lifecycle
of communication with SIYI gimbal cameras including:
- Connection management and auto-reconnect
- Request-response with timeout and retry logic
- Stream subscriptions for pushed data (attitude, laser, etc.)
- Automatic heartbeat for TCP transports
- Per-command concurrency control
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, Literal, TypeVar

import structlog

from siyi_sdk import commands
from siyi_sdk.constants import (
    CMD_AI_TRACK_STREAM,
    CMD_CAPTURE_PHOTO_RECORD_VIDEO,
    CMD_FUNCTION_FEEDBACK,
    CMD_REQUEST_GIMBAL_ATTITUDE,
    CMD_REQUEST_LASER_DISTANCE,
    CMD_REQUEST_MAGNETIC_ENCODER,
    CMD_REQUEST_MOTOR_VOLTAGE,
    CMD_SEND_AIRCRAFT_ATTITUDE,
    CMD_SEND_RAW_GPS,
    CMD_SEND_RC_CHANNELS,
    HEARTBEAT_FRAME,
)
from siyi_sdk.exceptions import (
    ConnectionError,
    NotConnectedError,
    TimeoutError,
)
from siyi_sdk.models import (
    AircraftAttitude,
    AIStreamStatus,
    AITrackingTarget,
    CameraSystemInfo,
    CaptureFuncType,
    CenteringAction,
    ControlMode,
    DataStreamFreq,
    EncodingParams,
    EnvCorrectionParams,
    FCDataType,
    FileNameType,
    FileType,
    FirmwareVersion,
    FunctionFeedback,
    GimbalAttitude,
    GimbalDataType,
    GimbalMotionMode,
    GimbalSystemInfo,
    HardwareID,
    IPConfig,
    IRThreshParams,
    IRThreshPrecision,
    LaserDistance,
    LaserTargetLatLon,
    MagneticEncoderAngles,
    MotorVoltage,
    PseudoColor,
    RawGPS,
    RCChannels,
    SetAttitudeAck,
    StreamType,
    SystemTime,
    TempGlobal,
    TempMeasureFlag,
    TempPoint,
    TempRegion,
    ThermalGain,
    ThermalOutputMode,
    VideoStitchingMode,
    WeakControlThreshold,
    ZoomRange,
)
from siyi_sdk.protocol.frame import Frame
from siyi_sdk.protocol.parser import FrameParser
from siyi_sdk.transport.base import AbstractTransport, Unsubscribe

if TYPE_CHECKING:
    from siyi_sdk.stream import SIYIStream

_T = TypeVar("_T")

logger: Final = structlog.get_logger(__name__)

# Fire-and-forget commands (no ACK expected)
_FIRE_AND_FORGET: Final[frozenset[int]] = frozenset(
    {
        CMD_CAPTURE_PHOTO_RECORD_VIDEO,
        CMD_SEND_AIRCRAFT_ATTITUDE,
        CMD_SEND_RC_CHANNELS,
        CMD_SEND_RAW_GPS,
    }
)

# Commands eligible for retry (idempotent reads + angle-target writes)
_IDEMPOTENT_READS: Final[frozenset[int]] = frozenset(
    {
        0x01,  # REQUEST_FIRMWARE_VERSION
        0x02,  # REQUEST_HARDWARE_ID
        0x0A,  # REQUEST_CAMERA_SYSTEM_INFO
        0x08,  # ONE_KEY_CENTERING (idempotent: centering twice = centering once)
        0x0D,  # REQUEST_GIMBAL_ATTITUDE
        0x0E,  # SET_ATTITUDE (idempotent: re-sending same target angle is safe)
        0x10,  # REQUEST_VIDEO_STITCHING_MODE
        0x15,  # REQUEST_LASER_DISTANCE
        0x16,  # REQUEST_ZOOM_RANGE
        0x17,  # REQUEST_LASER_LATLON
        0x18,  # REQUEST_ZOOM_MAGNIFICATION
        0x19,  # REQUEST_GIMBAL_MODE
        0x1A,  # REQUEST_PSEUDO_COLOR
        0x20,  # REQUEST_ENCODING_PARAMS
        0x26,  # REQUEST_MAGNETIC_ENCODER
        0x27,  # REQUEST_CONTROL_MODE
        0x28,  # REQUEST_WEAK_THRESHOLD
        0x2A,  # REQUEST_MOTOR_VOLTAGE
        0x31,  # REQUEST_GIMBAL_SYSTEM_INFO
        0x33,  # REQUEST_THERMAL_OUTPUT_MODE
        0x37,  # REQUEST_THERMAL_GAIN
        0x39,  # REQUEST_ENV_CORRECTION_PARAMS
        0x3B,  # REQUEST_ENV_CORRECTION_SWITCH
        0x40,  # REQUEST_SYSTEM_TIME
        0x41,  # SET_SINGLE_AXIS (idempotent: same target angle)
        0x42,  # GET_IR_THRESH_MAP_STA
        0x44,  # GET_IR_THRESH_PARAM
        0x46,  # GET_IR_THRESH_PRECISION
        0x49,  # GET_PIC_NAME_TYPE
        0x4B,  # GET_MAVLINK_OSD_FLAG
        0x4D,  # GET_AI_MODE_STA
        0x4E,  # GET_AI_TRACK_STREAM_STA
        0x70,  # REQUEST_WEAK_CONTROL_MODE
        0x81,  # GET_IP
    }
)

# Stream push commands (unsolicited frames from device)
_STREAM_PUSH_CMDS: Final[frozenset[int]] = frozenset(
    {
        CMD_REQUEST_GIMBAL_ATTITUDE,  # 0x0D
        CMD_FUNCTION_FEEDBACK,  # 0x0B
        CMD_REQUEST_LASER_DISTANCE,  # 0x15
        CMD_REQUEST_MAGNETIC_ENCODER,  # 0x26
        CMD_REQUEST_MOTOR_VOLTAGE,  # 0x2A
        CMD_AI_TRACK_STREAM,  # 0x50
    }
)


class SIYIClient:
    """High-level async client for SIYI gimbal cameras.

    This client manages connection lifecycle, request-response patterns,
    stream subscriptions, and automatic reconnection.

    Example:
        >>> async with SIYIClient(transport) as client:
        ...     version = await client.get_firmware_version()
        ...     print(version)

    Args:
        transport: Transport instance (UDP, TCP, Serial, or Mock).
        default_timeout: Default timeout for commands in seconds.
        max_retries: Maximum retry attempts for idempotent reads.
        retry_base_delay: Base delay for retry backoff in seconds.
        auto_reconnect: Enable automatic reconnection on transport failure.
        logger: Optional logger instance (uses module logger if None).
    """

    def __init__(
        self,
        transport: AbstractTransport,
        *,
        default_timeout: float = 2.0,
        max_retries: int = 2,
        retry_base_delay: float = 0.1,
        auto_reconnect: bool = False,
        response_matching: Literal["sequence", "command"] = "sequence",
    ) -> None:
        """Initialize the SIYI client.

        Args:
            transport: Transport instance.
            default_timeout: Default command timeout in seconds.
            max_retries: Maximum retries for idempotent reads.
            retry_base_delay: Base delay for exponential backoff.
            auto_reconnect: Enable automatic reconnection.
            response_matching: Match command and sequence, or command only for compatibility.
        """
        if response_matching not in ("sequence", "command"):
            raise ValueError("response_matching must be 'sequence' or 'command'")
        self._response_matching = response_matching
        self._lifecycle_lock = asyncio.Lock()
        self._closing = False
        self._supervisor_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._pending_meta: dict[int, tuple[int, bytes]] = {}
        self._transport = transport
        self._default_timeout = default_timeout
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._auto_reconnect = auto_reconnect
        self._logger = logger

        # Sequence number state
        self._seq: int = 0
        self._seq_lock = asyncio.Lock()

        # Pending requests registry (keyed by CMD_ID, not SEQ)
        self._pending: dict[int, asyncio.Future[Frame]] = {}
        self._cmd_locks: dict[int, asyncio.Lock] = {}

        # Stream subscription callbacks
        self._attitude_callbacks: list[Callable[[GimbalAttitude], None]] = []
        self._function_feedback_callbacks: list[Callable[[FunctionFeedback], None]] = []
        self._laser_callbacks: list[Callable[[LaserDistance], None]] = []
        self._ai_tracking_callbacks: list[Callable[[AITrackingTarget], None]] = []

        # Active stream subscriptions (for replay on reconnect)
        self._fc_streams: dict[FCDataType, DataStreamFreq] = {}
        self._gimbal_streams: dict[GimbalDataType, DataStreamFreq] = {}

        # Background tasks
        self._reader_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

        # Parser state
        self._parser = FrameParser()

        # Connection event (for reconnect notifications)
        self.connection_event = asyncio.Event()

    async def __aenter__(self) -> SIYIClient:
        """Enter async context manager.

        Returns:
            Self for use in async with statement.
        """
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Exit async context manager.

        Args:
            exc_info: Exception info (ignored).
        """
        await self.close()

    async def connect(self) -> None:
        """Connect and start one supervised reader/heartbeat pair."""
        async with self._lifecycle_lock:
            if self.connection_event.is_set():
                return
            if self._supervisor_task and not self._supervisor_task.done():
                raise NotConnectedError("Client is reconnecting")
            self._closing = False
            try:
                await self._open_session(replay=True)
            except BaseException:
                await self._cleanup_session()
                raise
            self._supervisor_task = asyncio.create_task(self._supervise(), name="siyi-supervisor")

    async def close(self) -> None:
        """Idempotently release the transport even when background tasks fail."""
        self._closing = True
        self.connection_event.clear()
        if self._close_task is None or self._close_task.done():
            self._close_task = asyncio.create_task(self._shutdown(), name="siyi-close")
        await asyncio.shield(self._close_task)

    async def _shutdown(self) -> None:
        async with self._lifecycle_lock:
            self._closing = True
            self.connection_event.clear()
            supervisor = self._supervisor_task
            if supervisor is not None:
                supervisor.cancel()
                await asyncio.gather(supervisor, return_exceptions=True)
                self._supervisor_task = None
            await self._cleanup_session()

    async def _open_session(self, *, replay: bool) -> None:
        self.connection_event.clear()
        self._parser.reset()
        await self._transport.connect()
        self._reader_task = asyncio.create_task(self._reader(), name="siyi-reader")
        if self._transport.supports_heartbeat:
            self._heartbeat_task = asyncio.create_task(self._heartbeat(), name="siyi-heartbeat")
        if replay and (self._fc_streams or self._gimbal_streams):
            replay_task = asyncio.create_task(self._replay_subscriptions())
            watched = [replay_task, self._reader_task]
            if self._heartbeat_task is not None:
                watched.append(self._heartbeat_task)
            try:
                done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
                if self._reader_task in done or self._heartbeat_task in done:
                    raise ConnectionError("Connection lost during subscription replay")
                await replay_task
            finally:
                replay_task.cancel()
                await asyncio.gather(replay_task, return_exceptions=True)
        if self._reader_task.done() or (self._heartbeat_task and self._heartbeat_task.done()):
            raise ConnectionError("Connection lost during subscription replay")
        self.connection_event.set()

    async def _replay_subscriptions(self) -> None:
        for data_type, freq in tuple(self._fc_streams.items()):
            ack = await self._send_command(
                0x24, commands.encode_fc_stream(data_type, freq), _internal=True
            )
            commands.decode_fc_stream_ack(ack)
        for gimbal_type, freq in tuple(self._gimbal_streams.items()):
            ack = await self._send_command(
                0x25, commands.encode_gimbal_stream(gimbal_type, freq), _internal=True
            )
            commands.decode_gimbal_stream_ack(ack)

    async def _cleanup_session(self) -> None:
        self.connection_event.clear()
        for fut in tuple(self._pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError("Connection lost"))
        self._pending.clear()
        self._pending_meta.clear()
        tasks = [task for task in (self._reader_task, self._heartbeat_task) if task is not None]
        for task in tasks:
            task.cancel()
        failures = await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = self._heartbeat_task = None
        try:
            await self._transport.close()
        except Exception as exc:
            self._logger.error("transport_close_failed", error=str(exc))
        finally:
            self._parser.reset()
        for failure in failures:
            if isinstance(failure, Exception):
                self._logger.warning("connection_task_failed", error=str(failure))

    async def _supervise(self) -> None:
        try:
            while not self._closing:
                tasks = [
                    task for task in (self._reader_task, self._heartbeat_task) if task is not None
                ]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                await self._cleanup_session()
                if not self._auto_reconnect or self._closing:
                    return
                if not await self._reconnect():
                    return
        finally:
            await self._cleanup_session()

    async def _reconnect(self) -> bool:
        for attempt, delay in enumerate((0.5, 1.0, 2.0, 4.0, 8.0), 1):
            await asyncio.sleep(delay)
            try:
                await self._open_session(replay=True)
                return True
            except Exception as exc:
                await self._cleanup_session()
                self._logger.warning("reconnect_failed", attempt=attempt, error=str(exc))
        self._logger.error("reconnect_exhausted")
        return False

    def _next_seq(self) -> int:
        current = self._seq
        self._seq = (self._seq + 1) & 0xFFFF
        return current

    def _require_connected(self, internal: bool = False) -> None:
        if (
            self._closing
            or not self._transport.is_connected
            or (not internal and not self.connection_event.is_set())
        ):
            raise NotConnectedError("Client is not connected (or is reconnecting)")

    async def _send_command(
        self,
        cmd_id: int,
        payload: bytes,
        *,
        expect_response: bool = True,
        timeout: float | None = None,
        max_retries: int | None = None,
        _internal: bool = False,
    ) -> bytes:
        self._require_connected(_internal)
        timeout = self._default_timeout if timeout is None else timeout
        lock = self._cmd_locks.setdefault(cmd_id, asyncio.Lock())
        async with lock:
            retries = self._max_retries if max_retries is None else max_retries
            attempts = retries + 1 if expect_response and cmd_id in _IDEMPOTENT_READS else 1
            for attempt in range(attempts):
                self._require_connected(_internal)
                seq = self._next_seq()
                frame = Frame.build(cmd_id, payload, seq=seq, need_ack=expect_response)
                if not expect_response:
                    await self._transport.send(frame.to_bytes())
                    return b""
                fut: asyncio.Future[Frame] = asyncio.get_running_loop().create_future()
                self._pending[cmd_id] = fut
                self._pending_meta[cmd_id] = (seq, payload)
                retry = False
                try:
                    await self._transport.send(frame.to_bytes())
                    ack = await asyncio.wait_for(fut, timeout)
                    self._logger.debug("rx_ack", cmd_id=cmd_id, seq=seq, payload_len=len(ack.data))
                    return ack.data
                except asyncio.TimeoutError as exc:
                    if attempt == attempts - 1:
                        self._logger.warning("timeout_exhausted", cmd_id=cmd_id, timeout_s=timeout)
                        raise TimeoutError(cmd_id=cmd_id, timeout_s=timeout) from exc
                    self._logger.warning("timeout_retrying", cmd_id=cmd_id, attempt=attempt + 1)
                    retry = True
                finally:
                    if self._pending.get(cmd_id) is fut:
                        self._pending.pop(cmd_id, None)
                        self._pending_meta.pop(cmd_id, None)
                    if not fut.done():
                        fut.cancel()
                    elif not fut.cancelled():
                        fut.exception()  # Consume failures when cancellation races transport loss.
                if retry:
                    await asyncio.sleep(self._retry_base_delay * 2**attempt)
        return b""

    async def _reader(self) -> None:
        async for chunk in self._transport.stream():
            parsed = self._parser.feed(chunk)
            for error in parsed.errors:
                self._logger.warning("parse_error_skipped", error=str(error))
            for frame in parsed.frames:
                await self._dispatch_frame(frame)
        raise ConnectionError("Transport reader reached EOF")

    async def _dispatch_frame(self, frame: Frame) -> None:
        fut = self._pending.get(frame.cmd_id)
        meta = self._pending_meta.get(frame.cmd_id)
        if fut is not None and not fut.done() and meta is not None:
            seq, payload = meta
            sequence_ok = self._response_matching == "command" or seq == frame.seq
            # These responses echo the requested stream/file selector in byte zero.
            selector_ok = frame.cmd_id not in (0x20, 0x21, 0x24, 0x25, 0x49, 0x4A) or bool(
                payload and frame.data and payload[0] == frame.data[0]
            )
            if sequence_ok and selector_ok:
                fut.set_result(frame)
        if frame.cmd_id in _STREAM_PUSH_CMDS:
            await self._dispatch_stream(frame)
        elif fut is None:
            self._logger.warning("unexpected_frame", cmd_id=f"0x{frame.cmd_id:02X}")

    def _notify_subscribers(self, callbacks: list[Callable[[_T], None]], value: _T) -> None:
        for callback in tuple(callbacks):
            try:
                callback(value)
            except Exception as exc:
                self._logger.error("stream_callback_failed", error=str(exc))

    async def _dispatch_stream(self, frame: Frame) -> None:
        try:
            if frame.cmd_id == CMD_REQUEST_GIMBAL_ATTITUDE:
                self._notify_subscribers(
                    self._attitude_callbacks, commands.decode_gimbal_attitude(frame.data)
                )
            elif frame.cmd_id == CMD_FUNCTION_FEEDBACK:
                self._notify_subscribers(
                    self._function_feedback_callbacks, commands.decode_function_feedback(frame.data)
                )
            elif frame.cmd_id == CMD_REQUEST_LASER_DISTANCE:
                self._notify_subscribers(
                    self._laser_callbacks, commands.decode_laser_distance(frame.data)
                )
            elif frame.cmd_id == CMD_AI_TRACK_STREAM:
                self._notify_subscribers(
                    self._ai_tracking_callbacks, commands.decode_ai_tracking(frame.data)
                )
            elif frame.cmd_id == CMD_REQUEST_MAGNETIC_ENCODER:
                self._logger.debug(
                    "magnetic_encoder_push", value=commands.decode_magnetic_encoder(frame.data)
                )
            elif frame.cmd_id == CMD_REQUEST_MOTOR_VOLTAGE:
                self._logger.debug(
                    "motor_voltage_push", value=commands.decode_motor_voltage(frame.data)
                )
        except Exception as exc:
            self._logger.warning("stream_decode_failed", cmd_id=frame.cmd_id, error=str(exc))

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            await self._transport.send(HEARTBEAT_FRAME)

    # =========================================================================
    # System Commands (0x00, 0x01, 0x02, 0x40, 0x30, 0x31, 0x80, 0x81, 0x82)
    # =========================================================================

    async def heartbeat(self) -> None:
        """Send a TCP heartbeat frame.

        Note:
            Heartbeat is sent automatically for TCP transports.
            This method is provided for manual control if needed.
        """
        self._require_connected()
        await self._transport.send(HEARTBEAT_FRAME)

    async def get_firmware_version(self) -> FirmwareVersion:
        """Request firmware version information.

        Returns:
            Firmware version data.
        """
        payload = commands.encode_firmware_version()
        ack = await self._send_command(0x01, payload)
        return commands.decode_firmware_version(ack)

    async def get_hardware_id(self) -> HardwareID:
        """Request hardware ID.

        Returns:
            Hardware identification data.
        """
        payload = commands.encode_hardware_id()
        ack = await self._send_command(0x02, payload)
        return commands.decode_hardware_id(ack)

    async def get_system_time(self) -> SystemTime:
        """Request system time.

        Returns:
            System time data.
        """
        payload = commands.encode_system_time()
        ack = await self._send_command(0x40, payload)
        return commands.decode_system_time(ack)

    async def set_utc_time(self, unix_usec: int) -> bool:
        """Set system UTC time.

        Args:
            unix_usec: UNIX epoch time in microseconds.

        Returns:
            True if successful.
        """
        payload = commands.encode_set_utc_time(unix_usec)
        ack = await self._send_command(0x30, payload)
        result = commands.decode_set_utc_time_ack(ack)
        return result

    async def get_gimbal_system_info(self) -> GimbalSystemInfo:
        """Request gimbal system information.

        Returns:
            Gimbal system info.
        """
        payload = commands.encode_gimbal_system_info()
        ack = await self._send_command(0x31, payload)
        return commands.decode_gimbal_system_info(ack)

    async def soft_reboot(self, *, camera: bool = False, gimbal: bool = False) -> tuple[bool, bool]:
        """Soft reboot camera and/or gimbal.

        Args:
            camera: Reboot camera module.
            gimbal: Reboot gimbal module.

        Returns:
            Tuple of (camera_rebooted, gimbal_rebooted).
        """
        payload = commands.encode_soft_reboot(camera=camera, gimbal=gimbal)
        ack = await self._send_command(0x80, payload)
        return commands.decode_soft_reboot_ack(ack)

    async def get_ip_config(self) -> IPConfig:
        """Request IP configuration.

        Returns:
            IP configuration.
        """
        payload = commands.encode_get_ip()
        ack = await self._send_command(0x81, payload)
        return commands.decode_get_ip(ack)

    async def set_ip_config(self, cfg: IPConfig) -> None:
        """Set IP configuration.

        Args:
            cfg: New IP configuration.
        """
        payload = commands.encode_set_ip(cfg)
        ack = await self._send_command(0x82, payload)
        commands.decode_set_ip_ack(ack)

    # =========================================================================
    # Focus / Zoom (0x04, 0x05, 0x06, 0x0F, 0x16, 0x18)
    # =========================================================================

    async def auto_focus(self, touch_x: int, touch_y: int) -> None:
        """Trigger auto-focus at specified touch coordinates.

        Args:
            touch_x: X coordinate (0-65535).
            touch_y: Y coordinate (0-65535).
        """
        payload = commands.encode_auto_focus(1, touch_x, touch_y)
        ack = await self._send_command(0x04, payload)
        commands.decode_auto_focus_ack(ack)

    async def manual_zoom(self, direction: int) -> float:
        """Perform manual zoom with auto-focus.

        Args:
            direction: Zoom direction (-1=out, 0=stop, 1=in).

        Returns:
            Current zoom magnification after zoom.
        """
        payload = commands.encode_manual_zoom(direction)
        try:
            ack = await self._send_command(0x05, payload)
            return commands.decode_manual_zoom_ack(ack)
        except TimeoutError:
            if direction == 0:
                # Some cameras (e.g. A8 mini) don't ACK the stop command when
                # not actively zooming. Query current zoom as fallback.
                return await self.get_current_zoom()
            raise

    async def manual_focus(self, direction: int) -> None:
        """Perform manual focus.

        Args:
            direction: Focus direction (-1=far, 0=stop, 1=near).
        """
        payload = commands.encode_manual_focus(direction)
        ack = await self._send_command(0x06, payload)
        commands.decode_manual_focus_ack(ack)

    async def manual_zoom_nowait(self, direction: int) -> None:
        """Send continuous zoom velocity without waiting for an acknowledgment."""
        payload = commands.encode_manual_zoom(direction)
        await self._send_command(0x05, payload, expect_response=False)

    async def manual_focus_nowait(self, direction: int) -> None:
        """Send continuous focus velocity without waiting for an acknowledgment."""
        payload = commands.encode_manual_focus(direction)
        await self._send_command(0x06, payload, expect_response=False)

    async def absolute_zoom(self, zoom: float) -> None:
        """Set absolute zoom level with auto-focus.

        Args:
            zoom: Target zoom magnification.
        """
        payload = commands.encode_absolute_zoom(zoom)
        ack = await self._send_command(0x0F, payload)
        commands.decode_absolute_zoom_ack(ack)

    async def get_zoom_range(self) -> ZoomRange:
        """Request zoom range capabilities.

        Returns:
            Zoom range information.
        """
        payload = commands.encode_zoom_range()
        ack = await self._send_command(0x16, payload)
        return commands.decode_zoom_range(ack)

    async def get_current_zoom(self) -> float:
        """Request current zoom magnification.

        Returns:
            Current zoom level.
        """
        payload = commands.encode_current_zoom()
        ack = await self._send_command(0x18, payload)
        current = commands.decode_current_zoom(ack)
        return current.zoom

    # =========================================================================
    # Gimbal (0x07, 0x08, 0x0E, 0x19, 0x41)
    # =========================================================================

    async def rotate(self, yaw: int, pitch: int) -> None:
        """Rotate gimbal with velocity control.

        Args:
            yaw: Yaw velocity (-100 to 100).
            pitch: Pitch velocity (-100 to 100).
        """
        payload = commands.encode_rotation(yaw, pitch)
        ack = await self._send_command(0x07, payload)
        commands.decode_rotation_ack(ack)

    async def one_key_centering(self, action: CenteringAction = CenteringAction.CENTER) -> None:
        """Execute one-key centering action.

        Args:
            action: Centering action type.
        """
        payload = commands.encode_one_key_centering(action)
        ack = await self._send_command(0x08, payload)
        commands.decode_one_key_centering_ack(ack)

    async def set_attitude(self, yaw_deg: float, pitch_deg: float) -> SetAttitudeAck:
        """Set gimbal attitude angles.

        Args:
            yaw_deg: Target yaw angle in degrees.
            pitch_deg: Target pitch angle in degrees.

        Returns:
            Acknowledgment with current attitude.
        """
        payload = commands.encode_set_attitude(yaw_deg, pitch_deg)
        ack = await self._send_command(0x0E, payload)
        return commands.decode_set_attitude_ack(ack)

    async def set_single_axis(
        self, axis: Literal["yaw", "pitch"], angle_deg: float
    ) -> SetAttitudeAck:
        """Set single axis attitude (A8 mini responds with 0x0E ACK).

        Args:
            axis: Axis to control ("yaw" or "pitch").
            angle_deg: Target angle in degrees.

        Returns:
            Acknowledgment with current attitude.
        """
        axis_int = 0 if axis == "yaw" else 1
        payload = commands.encode_single_axis(angle_deg, axis_int)
        ack = await self._send_command(0x41, payload)
        return commands.decode_single_axis_ack(ack)

    async def rotate_nowait(self, yaw: int, pitch: int) -> None:
        """Send a gimbal velocity command without waiting for an ACK.

        Intended for high-rate control loops (e.g. visual servoing at
        50-100 Hz) where ACK round-trip latency and per-CMD_ID serialisation
        would stall the sender. The standard `rotate()` is preferred for
        one-shot commands where you want confirmation.

        Args:
            yaw: Yaw velocity (-100 to 100, 0 = stop).
            pitch: Pitch velocity (-100 to 100, 0 = stop).
        """
        payload = commands.encode_rotation(yaw, pitch)
        await self._send_command(0x07, payload, expect_response=False)

    async def set_attitude_nowait(self, yaw_deg: float, pitch_deg: float) -> None:
        """Send a gimbal position setpoint without waiting for an ACK.

        Fire-and-forget variant of `set_attitude()`. The ACK from 0x0E
        only reports the gimbal's attitude at receipt time (not the
        achieved target), so for closed-loop control you should rely on
        the attitude push stream (`on_attitude`) for feedback instead.

        Args:
            yaw_deg: Target yaw angle in degrees.
            pitch_deg: Target pitch angle in degrees.
        """
        payload = commands.encode_set_attitude(yaw_deg, pitch_deg)
        await self._send_command(0x0E, payload, expect_response=False)

    async def set_single_axis_nowait(self, axis: Literal["yaw", "pitch"], angle_deg: float) -> None:
        """Send a single-axis position setpoint without waiting for an ACK.

        Args:
            axis: Axis to control ("yaw" or "pitch").
            angle_deg: Target angle in degrees.
        """
        axis_int = 0 if axis == "yaw" else 1
        payload = commands.encode_single_axis(angle_deg, axis_int)
        await self._send_command(0x41, payload, expect_response=False)

    async def get_gimbal_mode(self) -> GimbalMotionMode:
        """Request current gimbal motion mode.

        Returns:
            Gimbal motion mode.
        """
        payload = commands.encode_gimbal_mode()
        ack = await self._send_command(0x19, payload)
        return commands.decode_gimbal_mode(ack)

    # =========================================================================
    # Attitude / Streams (0x0D, 0x22, 0x24, 0x25, 0x26, 0x3E)
    # =========================================================================

    async def get_gimbal_attitude(self) -> GimbalAttitude:
        """Request gimbal attitude.

        Returns:
            Gimbal attitude data.
        """
        payload = commands.encode_gimbal_attitude()
        ack = await self._send_command(0x0D, payload)
        return commands.decode_gimbal_attitude(ack)

    async def send_aircraft_attitude(self, att: AircraftAttitude) -> None:
        """Send aircraft attitude to gimbal (fire-and-forget).

        Args:
            att: Aircraft attitude data.
        """
        payload = commands.encode_aircraft_attitude(att)
        await self._send_command(0x22, payload, expect_response=False)

    async def request_fc_stream(self, data_type: FCDataType, freq: DataStreamFreq) -> None:
        """Request flight controller data stream.

        Args:
            data_type: Type of FC data to stream.
            freq: Stream frequency.
        """
        payload = commands.encode_fc_stream(data_type, freq)
        ack = await self._send_command(0x24, payload)
        commands.decode_fc_stream_ack(ack)

        # Track active streams for reconnect
        if freq == DataStreamFreq.OFF:
            self._fc_streams.pop(data_type, None)
        else:
            self._fc_streams[data_type] = freq

    async def request_gimbal_stream(self, data_type: GimbalDataType, freq: DataStreamFreq) -> None:
        """Request gimbal data stream (subscribes to pushes).

        Args:
            data_type: Type of gimbal data to stream.
            freq: Stream frequency.
        """
        payload = commands.encode_gimbal_stream(data_type, freq)
        ack = await self._send_command(0x25, payload)
        commands.decode_gimbal_stream_ack(ack)

        # Track active streams for reconnect
        if freq == DataStreamFreq.OFF:
            self._gimbal_streams.pop(data_type, None)
        else:
            self._gimbal_streams[data_type] = freq

    async def get_magnetic_encoder(self) -> MagneticEncoderAngles:
        """Request magnetic encoder angles.

        Returns:
            Magnetic encoder angles.
        """
        payload = commands.encode_magnetic_encoder()
        ack = await self._send_command(0x26, payload)
        return commands.decode_magnetic_encoder(ack)

    async def send_raw_gps(self, gps: RawGPS) -> None:
        """Send raw GPS data to gimbal (fire-and-forget, ZR10/ZR30/A8 only).

        Args:
            gps: Raw GPS data.
        """
        payload = commands.encode_raw_gps(gps)
        await self._send_command(0x3E, payload, expect_response=False)

    def on_attitude(self, cb: Callable[[GimbalAttitude], None]) -> Unsubscribe:
        """Subscribe to attitude stream pushes.

        Args:
            cb: Callback to invoke on each attitude frame.

        Returns:
            Unsubscribe callable.
        """
        self._attitude_callbacks.append(cb)

        def unsubscribe() -> None:
            if cb in self._attitude_callbacks:
                self._attitude_callbacks.remove(cb)

        return unsubscribe

    def on_laser_distance(self, cb: Callable[[LaserDistance], None]) -> Unsubscribe:
        """Subscribe to laser distance stream pushes.

        Args:
            cb: Callback to invoke on each laser distance frame.

        Returns:
            Unsubscribe callable.
        """
        self._laser_callbacks.append(cb)

        def unsubscribe() -> None:
            if cb in self._laser_callbacks:
                self._laser_callbacks.remove(cb)

        return unsubscribe

    # =========================================================================
    # Camera (0x0A, 0x0B, 0x0C, 0x20, 0x21, 0x48, 0x49, 0x4A, 0x4B, 0x4C)
    # =========================================================================

    async def get_camera_system_info(
        self, *, timeout: float | None = None, max_retries: int | None = None
    ) -> CameraSystemInfo:
        """Request camera system information.

        Args:
            timeout: Override reply timeout in seconds.
            max_retries: Override retries; use zero for a single RTT sample.

        Returns:
            Camera system info.
        """
        payload = commands.encode_camera_system_info()
        ack = await self._send_command(0x0A, payload, timeout=timeout, max_retries=max_retries)
        return commands.decode_camera_system_info(ack)

    def on_function_feedback(self, cb: Callable[[FunctionFeedback], None]) -> Unsubscribe:
        """Subscribe to function feedback stream pushes.

        Args:
            cb: Callback to invoke on each function feedback.

        Returns:
            Unsubscribe callable.
        """
        self._function_feedback_callbacks.append(cb)

        def unsubscribe() -> None:
            if cb in self._function_feedback_callbacks:
                self._function_feedback_callbacks.remove(cb)

        return unsubscribe

    async def capture(self, func: CaptureFuncType) -> None:
        """Capture photo or record video (fire-and-forget).

        Args:
            func: Capture function type.
        """
        payload = commands.encode_capture(func)
        await self._send_command(0x0C, payload, expect_response=False)

    async def get_encoding_params(self, stream: StreamType) -> EncodingParams:
        """Request video encoding parameters.

        Args:
            stream: Stream type.

        Returns:
            Encoding parameters.
        """
        payload = commands.encode_get_encoding_params(stream)
        ack = await self._send_command(0x20, payload)
        return commands.decode_get_encoding_params(ack)

    async def set_encoding_params(self, params: EncodingParams) -> bool:
        """Set video encoding parameters.

        Args:
            params: New encoding parameters.

        Returns:
            True if successful.
        """
        payload = commands.encode_set_encoding_params(params)
        ack = await self._send_command(0x21, payload)
        return commands.decode_set_encoding_params_ack(ack)

    async def format_sd_card(self) -> bool:
        """Format SD card (ZT30/ZR30/A8 may not respond).

        Returns:
            True if acknowledged.
        """
        payload = commands.encode_format_sd()
        ack = await self._send_command(0x48, payload)
        return commands.decode_format_sd_ack(ack)

    async def get_picture_name_type(self, ft: FileType) -> FileNameType:
        """Request file naming convention type.

        Args:
            ft: File type.

        Returns:
            File name type.
        """
        payload = commands.encode_get_pic_name_type(ft)
        ack = await self._send_command(0x49, payload)
        return commands.decode_get_pic_name_type(ack)

    async def set_picture_name_type(self, ft: FileType, nt: FileNameType) -> None:
        """Set file naming convention type.

        Args:
            ft: File type.
            nt: File name type.
        """
        payload = commands.encode_set_pic_name_type(ft, nt)
        ack = await self._send_command(0x4A, payload)
        commands.decode_set_pic_name_type_ack(ack)

    async def get_osd_flag(self) -> bool:
        """Request OSD overlay flag.

        Returns:
            True if OSD is enabled.
        """
        payload = commands.encode_get_osd_flag()
        ack = await self._send_command(0x4B, payload)
        return commands.decode_get_osd_flag(ack)

    async def set_osd_flag(self, on: bool) -> bool:
        """Set OSD overlay flag.

        Args:
            on: Enable OSD overlay.

        Returns:
            True if successful.
        """
        payload = commands.encode_set_osd_flag(on)
        ack = await self._send_command(0x4C, payload)
        return commands.decode_set_osd_flag_ack(ack)

    # =========================================================================
    # Video stitching (0x10, 0x11)
    # =========================================================================

    async def get_video_stitching_mode(self) -> VideoStitchingMode:
        """Request video stitching mode.

        Returns:
            Video stitching mode.
        """
        payload = commands.encode_get_video_stitching_mode()
        ack = await self._send_command(0x10, payload)
        return commands.decode_video_stitching_mode(ack)

    async def set_video_stitching_mode(self, mode: VideoStitchingMode) -> VideoStitchingMode:
        """Set video stitching mode.

        Args:
            mode: Video stitching mode.

        Returns:
            New video stitching mode.
        """
        payload = commands.encode_set_video_stitching_mode(mode)
        ack = await self._send_command(0x11, payload)
        return commands.decode_set_video_stitching_mode_ack(ack)

    # =========================================================================
    # Thermal (0x12-0x14, 0x1A, 0x1B, 0x33-0x3C, 0x42-0x47, 0x4F)
    # =========================================================================

    async def temp_at_point(self, x: int, y: int, flag: TempMeasureFlag) -> TempPoint:
        """Measure temperature at a specific point.

        Args:
            x: X coordinate.
            y: Y coordinate.
            flag: Measurement mode.

        Returns:
            Temperature at point.
        """
        payload = commands.encode_temp_at_point(x, y, flag)
        ack = await self._send_command(0x12, payload)
        return commands.decode_temp_at_point(ack)

    async def temp_region(
        self, region: tuple[int, int, int, int], flag: TempMeasureFlag
    ) -> TempRegion:
        """Measure temperature in a rectangular region.

        Args:
            region: Tuple of (startx, starty, endx, endy).
            flag: Measurement mode.

        Returns:
            Temperature region data.
        """
        payload = commands.encode_local_temp(*region, flag)
        ack = await self._send_command(0x13, payload)
        return commands.decode_local_temp(ack)

    async def temp_global(self, flag: TempMeasureFlag) -> TempGlobal:
        """Measure global temperature across entire frame.

        Args:
            flag: Measurement mode.

        Returns:
            Global temperature data.
        """
        payload = commands.encode_global_temp(flag)
        ack = await self._send_command(0x14, payload)
        return commands.decode_global_temp(ack)

    async def get_pseudo_color(self) -> PseudoColor:
        """Request thermal pseudo-color palette.

        Returns:
            Pseudo-color setting.
        """
        payload = commands.encode_get_pseudo_color()
        ack = await self._send_command(0x1A, payload)
        return commands.decode_pseudo_color(ack)

    async def set_pseudo_color(self, c: PseudoColor) -> PseudoColor:
        """Set thermal pseudo-color palette.

        Args:
            c: Pseudo-color palette.

        Returns:
            New pseudo-color setting.
        """
        payload = commands.encode_set_pseudo_color(c)
        ack = await self._send_command(0x1B, payload)
        return commands.decode_set_pseudo_color_ack(ack)

    async def get_thermal_output_mode(self) -> ThermalOutputMode:
        """Request thermal output mode.

        Returns:
            Thermal output mode.
        """
        payload = commands.encode_get_thermal_output_mode()
        ack = await self._send_command(0x33, payload)
        return commands.decode_thermal_output_mode(ack)

    async def set_thermal_output_mode(self, m: ThermalOutputMode) -> ThermalOutputMode:
        """Set thermal output mode.

        Args:
            m: Thermal output mode.

        Returns:
            New thermal output mode.
        """
        payload = commands.encode_set_thermal_output_mode(m)
        ack = await self._send_command(0x34, payload)
        return commands.decode_set_thermal_output_mode_ack(ack)

    async def get_single_temp_frame(self) -> bool:
        """Request single temperature frame.

        Returns:
            True if successful.
        """
        payload = commands.encode_get_single_temp_frame()
        ack = await self._send_command(0x35, payload)
        return commands.decode_single_temp_frame_ack(ack)

    async def get_thermal_gain(self) -> ThermalGain:
        """Request thermal gain mode.

        Returns:
            Thermal gain.
        """
        payload = commands.encode_get_thermal_gain()
        ack = await self._send_command(0x37, payload)
        return commands.decode_thermal_gain(ack)

    async def set_thermal_gain(self, g: ThermalGain) -> ThermalGain:
        """Set thermal gain mode.

        Args:
            g: Thermal gain.

        Returns:
            New thermal gain.
        """
        payload = commands.encode_set_thermal_gain(g)
        ack = await self._send_command(0x38, payload)
        return commands.decode_set_thermal_gain_ack(ack)

    async def get_env_correction_params(self) -> EnvCorrectionParams:
        """Request environmental correction parameters.

        Returns:
            Environmental correction parameters.
        """
        payload = commands.encode_get_env_correction_params()
        ack = await self._send_command(0x39, payload)
        return commands.decode_env_correction_params(ack)

    async def set_env_correction_params(self, p: EnvCorrectionParams) -> bool:
        """Set environmental correction parameters.

        Args:
            p: Environmental correction parameters.

        Returns:
            True if successful.
        """
        payload = commands.encode_set_env_correction_params(p)
        ack = await self._send_command(0x3A, payload)
        return commands.decode_set_env_correction_params_ack(ack)

    async def get_env_correction_switch(self) -> bool:
        """Request environmental correction switch state.

        Returns:
            True if enabled.
        """
        payload = commands.encode_get_env_correction_switch()
        ack = await self._send_command(0x3B, payload)
        return commands.decode_env_correction_switch(ack)

    async def set_env_correction_switch(self, on: bool) -> bool:
        """Set environmental correction switch state.

        Args:
            on: Enable environmental correction.

        Returns:
            True if successful.
        """
        payload = commands.encode_set_env_correction_switch(on)
        ack = await self._send_command(0x3C, payload)
        return commands.decode_set_env_correction_switch_ack(ack)

    async def get_ir_thresh_map_state(self) -> bool:
        """Request IR threshold map state.

        Returns:
            True if enabled.
        """
        payload = commands.encode_get_ir_thresh_map_state()
        ack = await self._send_command(0x42, payload)
        return commands.decode_ir_thresh_map_state(ack)

    async def set_ir_thresh_map_state(self, on: bool) -> bool:
        """Set IR threshold map state.

        Args:
            on: Enable IR threshold map.

        Returns:
            True if successful.
        """
        payload = commands.encode_set_ir_thresh_map_state(on)
        ack = await self._send_command(0x43, payload)
        return commands.decode_set_ir_thresh_map_state_ack(ack)

    async def get_ir_thresh_params(self) -> IRThreshParams:
        """Request IR threshold parameters.

        Returns:
            IR threshold parameters.
        """
        payload = commands.encode_get_ir_thresh_params()
        ack = await self._send_command(0x44, payload)
        return commands.decode_ir_thresh_params(ack)

    async def set_ir_thresh_params(self, p: IRThreshParams) -> bool:
        """Set IR threshold parameters.

        Args:
            p: IR threshold parameters.

        Returns:
            True if successful.
        """
        payload = commands.encode_set_ir_thresh_params(p)
        ack = await self._send_command(0x45, payload)
        return commands.decode_set_ir_thresh_params_ack(ack)

    async def get_ir_thresh_precision(self) -> IRThreshPrecision:
        """Request IR threshold precision.

        Returns:
            IR threshold precision.
        """
        payload = commands.encode_get_ir_thresh_precision()
        ack = await self._send_command(0x46, payload)
        return commands.decode_ir_thresh_precision(ack)

    async def set_ir_thresh_precision(self, p: IRThreshPrecision) -> IRThreshPrecision:
        """Set IR threshold precision.

        Args:
            p: IR threshold precision.

        Returns:
            New IR threshold precision.
        """
        payload = commands.encode_set_ir_thresh_precision(p)
        ack = await self._send_command(0x47, payload)
        return commands.decode_set_ir_thresh_precision_ack(ack)

    async def manual_thermal_shutter(self) -> bool:
        """Trigger manual thermal shutter calibration.

        Returns:
            True if successful.
        """
        payload = commands.encode_manual_thermal_shutter()
        ack = await self._send_command(0x4F, payload)
        return commands.decode_manual_thermal_shutter_ack(ack)

    # =========================================================================
    # Laser (0x15, 0x17, 0x32)
    # =========================================================================

    async def get_laser_distance(self) -> LaserDistance:
        """Request laser distance measurement.

        Returns:
            Laser distance data.
        """
        payload = commands.encode_laser_distance()
        ack = await self._send_command(0x15, payload)
        return commands.decode_laser_distance(ack)

    async def get_laser_target_latlon(self) -> LaserTargetLatLon:
        """Request laser target latitude/longitude.

        Returns:
            Laser target coordinates.
        """
        payload = commands.encode_laser_target_latlon()
        ack = await self._send_command(0x17, payload)
        return commands.decode_laser_target_latlon(ack)

    async def set_laser_ranging_state(self, on: bool) -> bool:
        """Set laser ranging state.

        Args:
            on: Enable laser ranging.

        Returns:
            True if successful.
        """
        payload = commands.encode_set_laser_ranging_state(on)
        ack = await self._send_command(0x32, payload)
        return commands.decode_set_laser_ranging_state_ack(ack)

    # =========================================================================
    # RC (0x23, 0x24)
    # =========================================================================

    async def send_rc_channels(self, ch: RCChannels) -> None:
        """Send RC channel data to gimbal (fire-and-forget, deprecated).

        Args:
            ch: RC channels data.
        """
        warnings.warn(
            "send_rc_channels is deprecated per SIYI SDK protocol",
            DeprecationWarning,
            stacklevel=2,
        )
        payload = commands.encode_rc_channels(ch)
        await self._send_command(0x23, payload, expect_response=False)

    # =========================================================================
    # AI (0x4D, 0x4E, 0x50, 0x51)
    # =========================================================================

    async def get_ai_mode(self) -> bool:
        """Request AI mode state.

        Returns:
            True if AI mode is enabled.
        """
        payload = commands.encode_get_ai_mode()
        ack = await self._send_command(0x4D, payload)
        return commands.decode_ai_mode(ack)

    async def get_ai_stream_status(self) -> AIStreamStatus:
        """Request AI tracking stream status.

        Returns:
            AI stream status.
        """
        payload = commands.encode_get_ai_stream_status()
        ack = await self._send_command(0x4E, payload)
        return commands.decode_ai_stream_status(ack)

    async def set_ai_stream_output(self, on: bool) -> bool:
        """Set AI tracking stream output.

        Args:
            on: Enable AI tracking stream.

        Returns:
            True if successful.
        """
        payload = commands.encode_set_ai_stream_output(on)
        ack = await self._send_command(0x51, payload)
        return commands.decode_set_ai_stream_output_ack(ack)

    def on_ai_tracking(self, cb: Callable[[AITrackingTarget], None]) -> Unsubscribe:
        """Subscribe to AI tracking stream pushes.

        Args:
            cb: Callback to invoke on each AI tracking target frame.

        Returns:
            Unsubscribe callable.
        """
        self._ai_tracking_callbacks.append(cb)

        def unsubscribe() -> None:
            if cb in self._ai_tracking_callbacks:
                self._ai_tracking_callbacks.remove(cb)

        return unsubscribe

    # =========================================================================
    # Debug / ArduPilot-only (0x27, 0x28, 0x29, 0x2A, 0x70, 0x71)
    # =========================================================================

    async def get_control_mode(self) -> ControlMode:
        """Request gimbal control mode (ArduPilot debugging).

        Returns:
            Control mode.
        """
        payload = commands.encode_get_control_mode()
        ack = await self._send_command(0x27, payload)
        return commands.decode_control_mode(ack)

    async def get_weak_threshold(self) -> WeakControlThreshold:
        """Request weak control threshold parameters.

        Returns:
            Weak control threshold.
        """
        payload = commands.encode_get_weak_threshold()
        ack = await self._send_command(0x28, payload)
        return commands.decode_weak_threshold(ack)

    async def set_weak_threshold(self, t: WeakControlThreshold) -> bool:
        """Set weak control threshold parameters.

        Args:
            t: Weak control threshold.

        Returns:
            True if successful.
        """
        payload = commands.encode_set_weak_threshold(t)
        ack = await self._send_command(0x29, payload)
        return commands.decode_set_weak_threshold_ack(ack)

    async def get_motor_voltage(self) -> MotorVoltage:
        """Request motor voltage data.

        Returns:
            Motor voltage.
        """
        payload = commands.encode_get_motor_voltage()
        ack = await self._send_command(0x2A, payload)
        return commands.decode_motor_voltage(ack)

    async def get_weak_control_mode(self) -> bool:
        """Request weak control mode state.

        Returns:
            True if weak control mode is enabled.
        """
        payload = commands.encode_get_weak_control_mode()
        ack = await self._send_command(0x70, payload)
        return commands.decode_weak_control_mode(ack)

    async def set_weak_control_mode(self, on: bool) -> bool:
        """Set weak control mode state.

        Args:
            on: Enable weak control mode.

        Returns:
            True if successful.
        """
        payload = commands.encode_set_weak_control_mode(on)
        ack = await self._send_command(0x71, payload)
        return commands.decode_set_weak_control_mode_ack(ack)

    # =========================================================================
    # Video Streaming
    # =========================================================================

    def create_stream(
        self,
        stream: Literal["main", "sub"] = "main",
        generation: object = None,
        backend: object = None,
        transport: Literal["tcp", "udp"] = "tcp",
        latency_ms: int = 100,
        reconnect_delay: float = 2.0,
        max_reconnect_attempts: int = 0,
        buffer_size: int = 1,
    ) -> SIYIStream:
        """Create an RTSP stream connected to this camera's IP address.

        The returned stream is not yet started; call ``await stream.start()``
        to begin receiving frames.

        Args:
            stream: "main" for primary high-resolution stream,
                    "sub" for secondary low-resolution stream (new-gen only).
            generation: CameraGeneration enum value; defaults to NEW (ZT30/ZT6+).
                        Pass CameraGeneration.OLD for ZR30/ZR10/A8Mini/A2Mini/R1M.
            backend: StreamBackend enum value; defaults to AUTO.
            transport: RTSP transport protocol; "tcp" or "udp".
            latency_ms: GStreamer rtspsrc latency in milliseconds.
            reconnect_delay: Initial reconnection back-off delay in seconds.
            max_reconnect_attempts: Maximum reconnection attempts; 0 = unlimited.
            buffer_size: OpenCV CAP_PROP_BUFFERSIZE value.

        Returns:
            A SIYIStream instance (not yet started).
        """
        from siyi_sdk.stream import SIYIStream, build_rtsp_url
        from siyi_sdk.stream.models import CameraGeneration as _CameraGeneration
        from siyi_sdk.stream.models import StreamBackend as _StreamBackend
        from siyi_sdk.stream.models import StreamConfig

        gen = _CameraGeneration.OLD if generation is None else _CameraGeneration(generation)
        bk = _StreamBackend.AUTO if backend is None else _StreamBackend(backend)

        # Retrieve the host IP from the underlying transport when available.
        host: str = getattr(self._transport, "_ip", "192.168.144.25")
        url = build_rtsp_url(host=host, stream=stream, generation=gen)
        return SIYIStream(
            StreamConfig(
                rtsp_url=url,
                backend=bk,
                transport=transport,
                latency_ms=latency_ms,
                reconnect_delay=reconnect_delay,
                max_reconnect_attempts=max_reconnect_attempts,
                buffer_size=buffer_size,
            )
        )
