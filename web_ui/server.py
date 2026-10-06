# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import asyncio
import logging
import json
import os
import time
import math
import itertools
from types import SimpleNamespace
from collections import deque
from ipaddress import IPv4Address
from typing import Optional, AsyncGenerator, Any, Literal
from contextlib import asynccontextmanager

import cv2
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from siyi_sdk import (
    SIYIClient,
    FirmwareTrackingClient,
    MediaClient,
    SIYIStream,
    StreamConfig,
    StreamBackend,
    MediaType,
    build_rtsp_url,
    configure_logging,
)
from siyi_sdk.exceptions import ConfigurationError, TimeoutError
from siyi_sdk.transport.udp import UDPTransport
from siyi_sdk.models import (
    CenteringAction,
    CaptureFuncType,
    GimbalDataType,
    DataStreamFreq,
    StreamType,
    FirmwareVersion,
    HardwareID
)
from web_ui.command_explorer import COMMANDS, execute
from web_ui.frame_tap import TapTransport
from web_ui.pointing import (
    AttitudeHistory, PointingConfig, center_for, clamp_attitude, screen_to_world,
)
from web_ui.lock_overlay import draw_firmware_command, draw_lock
from siyi_sdk.tracking import (
    CalibrationError, FirmwareLink, FirmwarePointLock, FrameMotionRecorder, GimbalPointLock, LockGains, LockState, LoopModel,
    calibrate_loop,
)

# Setup logging
configure_logging(level="INFO")
logger = logging.getLogger(__name__)

# Global state
GLOBAL_CONFIG = {
    "camera_ip": "192.168.144.25",
    "port": 8082,
}

class CameraState:
    def __init__(self):
        self.client: Optional[SIYIClient] = None
        self.transport: Optional[TapTransport] = None
        self.media: Optional[MediaClient] = None
        self.stream: Optional[SIYIStream] = None
        self.stream_backend = StreamBackend.AUTO
        self.latest_frame: Optional[bytes] = None
        self.last_frame_time = 0.0
        self.stream_error: Optional[str] = None
        self.frame_event = asyncio.Event()
        self.attitude = {"yaw": 0.0, "pitch": 0.0, "roll": 0.0}
        self.feedback = deque(maxlen=20)
        self.lock = asyncio.Lock()
        self.is_connected = False
        self.watchdog_task: Optional[asyncio.Task] = None
        self.connection_error: Optional[str] = None
        self.ip: Optional[str] = None
        self.firmware_version: Optional[FirmwareVersion] = None
        self.live_enabled = True
        self.stop_event = asyncio.Event() # For clean shutdown
        self.status_task: Optional[asyncio.Task] = None
        self.motion_task: Optional[asyncio.Task] = None
        self.status_lock = asyncio.Lock()
        self.action_lock = asyncio.Lock()
        self.camera_status = None
        self.status_time = 0.0
        self.status_error = None
        self.rtt_samples = deque(maxlen=100)
        self.rtt_failures = deque(maxlen=100)
        self.motion_deadlines = {}
        self.jpeg_ms = None
        self.attitude_time = 0.0
        self.actions = {}
        self.confirmation_tasks = {}
        self.action_counter = 0
        self.last_status_failure = None
        self.pointing = PointingConfig()
        self.attitude_history = AttitudeHistory()
        self.zoom: Optional[float] = None
        self.zoom_max: Optional[float] = None
        # Pose and zoom captured when a drag or wheel gesture began.
        self.gesture: Optional[dict] = None
        self.zoom_refresh_task: Optional[asyncio.Task] = None
        # Point lock steers the gimbal from the video; raw frames are kept to start a lock.
        self.point_lock: Optional[GimbalPointLock | FirmwarePointLock] = None
        # Firmware tracking channel (port 37256) frames and lock events, for the protocol export.
        self.tracking_log: deque = deque(maxlen=5000)
        self.tracking_log_ids = itertools.count(1)
        self.tracking_lock = asyncio.Lock()
        self.lock_reason: Optional[str] = None
        self.lock_exit_confirmed = True
        self.latest_image = None
        self.latest_image_time = 0.0
        self.lock_ms: Optional[float] = None
        self.lock_last_command = (0, 0)
        self.calibration_recorder = FrameMotionRecorder()
        self.calibrating = False
        self.preview_task: Optional[asyncio.Task] = None
        # In firmware steering mode: the camera's own low-latency 1280x720 stream on
        # port 37256, which replaces RTSP for the live view and the tracker.
        self.firmware_link: Optional[FirmwareLink] = None

    async def initialize(self, ip: str):
        async with self.lock:
            # Shutdown existing clients first
            await self.shutdown()
            
            # Reset state for new initialization
            self.stop_event.clear()
            self.feedback.clear()
            self.ip = ip
            self.is_connected = False
            
            logger.info(f"Initializing clients for IP: {ip}")
            transport = TapTransport(UDPTransport(ip))
            self.transport = transport
            # A8 Mini replies observed over UDP use their own sequence counter.
            # Match ACKs by command ID; the client serializes each command ID.
            self.client = SIYIClient(transport, max_retries=2)
            self.client.on_attitude(self._on_attitude)
            self.client.on_function_feedback(self._on_feedback)
            # Status uses its own command ID lock, with one attempt per query;
            # keep the same UDP socket used by telemetry and motion control.
            self.status_task = asyncio.create_task(self.poll_status())
            self.motion_task = asyncio.create_task(self.motion_watchdog())
            self.media = MediaClient(ip)
            
            # Initialize Video Stream
            rtsp_url = build_rtsp_url(host=ip)
            config = StreamConfig(
                rtsp_url=rtsp_url,
                backend=self.stream_backend,
                latency_ms=100,
                codec="h264",
            )
            self.stream = SIYIStream(config)
            self.stream.on_frame(self._on_rtsp_frame)
            
            if self.watchdog_task:
                self.watchdog_task.cancel()
            self.watchdog_task = asyncio.create_task(self.watchdog())

    async def watchdog(self):
        """Background task to monitor camera and auto-recover."""
        consecutive_failures = 0
        while True:
            try:
                if self.client and self.ip:
                    try:
                        # Attempt to connect and ping
                        if not self.is_connected:
                            await asyncio.wait_for(self.client.connect(), timeout=5.0)
                        
                        # Allow all three SDK attempts (2s each) and their backoff.
                        self.firmware_version = await asyncio.wait_for(self.client.get_firmware_version(), timeout=8.0)
                        self.connection_error = None
                        consecutive_failures = 0
                        
                        if not self.is_connected:
                            logger.info("Camera connection restored")
                            self.is_connected = True
                            consecutive_failures = 0
                            # Telemetry support must not gate camera connectivity.
                            try:
                                await asyncio.wait_for(
                                    self.client.request_gimbal_stream(GimbalDataType.ATTITUDE, DataStreamFreq.HZ50),
                                    timeout=2.0
                                )
                            except Exception as e:
                                logger.warning(f"Attitude subscription failed: {e}")
                            await self.refresh_zoom()
                    except Exception as e:
                        logger.debug(f"Watchdog ping failed: {e}")
                        self.connection_error = (
                            f"No camera SDK response from {self.ip} on UDP port 37260. "
                            "Ping and the RTSP video port do not verify this control connection."
                        )
                        consecutive_failures += 1
                        if consecutive_failures >= 2 and self.is_connected:
                            logger.warning("Camera connection lost (Watchdog)")
                            self.is_connected = False
                            await self.release_lock("Point lock released: camera disconnected", require_confirmed=False)
                            self.latest_frame = None
                            self.last_frame_time = 0.0
                            if self.stream:
                                await asyncio.wait_for(self.stream.stop(), timeout=3.0)

                    await self.sync_firmware_link()
                    # A video backend failure must not mark a responding camera offline.
                    # Retry video independently, including after a failed start.
                    if self.is_connected and self.live_enabled and self.stream and not self.stream.is_running:
                        try:
                            await self.toggle_stream(True)
                        except Exception as e:
                            logger.warning(f"Video stream unavailable: {e}")
                
                await asyncio.sleep(2) # Faster polling
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Watchdog loop crash: {e}")
                await asyncio.sleep(5)

    def _on_attitude(self, att):
        self.attitude_time = time.monotonic()
        self.attitude_history.add(self.attitude_time, att.yaw_deg, att.pitch_deg)
        self.attitude = {
            "yaw": att.yaw_deg,
            "pitch": att.pitch_deg,
            "roll": att.roll_deg
        }

    def _on_feedback(self, feedback):
        self.feedback.append({"event": feedback.name, "time": time.time()})

    def firmware_tracking_supported(self) -> bool:
        version = self.firmware_version
        return (version is not None and FirmwareVersion.decode_word(version.camera) == (0, 3, 7)
                and FirmwareVersion.decode_word(version.gimbal) == (0, 4, 9))

    async def sync_firmware_link(self) -> None:
        """Run the firmware link while firmware steering is selected on a supported camera."""
        wanted = (self.is_connected and self.ip is not None
                  and self.pointing.lock_control == "firmware" and self.firmware_tracking_supported())
        link = self.firmware_link
        if wanted and link is not None and link.ip == self.ip:
            return
        if link is not None:
            if isinstance(self.point_lock, FirmwarePointLock):
                await self.release_lock("Point lock released: firmware link stopped", require_confirmed=False)
            self.firmware_link = None
            await link.stop()
        if wanted:
            self.firmware_link = FirmwareLink(self.ip, on_frame=self._on_link_frame,
                                              trace=self.trace_tracking)
            self.firmware_link.start()

    async def _on_link_frame(self, image, arrival: float) -> None:
        await self._on_frame(SimpleNamespace(frame=image, timestamp=arrival))

    async def _on_rtsp_frame(self, frame):
        # While the firmware stream delivers, RTSP frames would mix two pictures with
        # different delays and sizes; use RTSP only until (or unless) it is ready.
        link = self.firmware_link
        if link is not None and link.ready:
            return
        await self._on_frame(frame)

    async def _on_frame(self, frame):
        received = time.monotonic()
        image = frame.frame
        self.latest_image = image
        self.latest_image_time = frame.timestamp
        self.calibration_recorder.add(image, frame.timestamp)
        lock = self.point_lock
        if lock is not None and lock.active:
            started = time.perf_counter()
            try:
                status = await lock.update(image, zoom=self.zoom or 1.0, timestamp=frame.timestamp)
            except Exception as e:
                logger.warning(f"Point lock update failed: {e}")
                await self.release_lock("Point lock stopped after an error")
            else:
                self.lock_ms = round((time.perf_counter() - started) * 1000, 1)
                if status.state is LockState.IDLE:
                    self.feedback.append({"event": "LOCK_LOST", "time": time.time()})
                else:
                    image = image.copy()  # keep the marker out of the frame the tracker reads
                    draw_lock(image, status)
                    if isinstance(lock, FirmwarePointLock):
                        draw_firmware_command(
                            image, status, lock.last_target,
                            time.monotonic() - lock.last_target_time, self.gimbal_rate(),
                        )
        # The browser preview is encoded in the background so it never holds up
        # the next frame's tracking; if an encode is still running, skip this one.
        if self.preview_task is None or self.preview_task.done():
            self.preview_task = asyncio.create_task(self._encode_preview(image, received))

    async def _encode_preview(self, image, received):
        started = time.perf_counter()
        height, width = image.shape[:2]
        if width > 1280:  # the preview never needs more; JPEG cost grows with pixels
            image = cv2.resize(image, (1280, round(height * 1280 / width)), interpolation=cv2.INTER_AREA)
        success, buffer = await asyncio.to_thread(
            cv2.imencode, '.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 80]
        )
        self.jpeg_ms = round((time.perf_counter() - started) * 1000, 1)
        if success:
            self.latest_frame = buffer.tobytes()
            self.last_frame_time = received
            self.stream_error = None
            self.frame_event.set()

    async def refresh_status(self):
        async with self.status_lock:
            client = self.client
            if not client:
                raise HTTPException(status_code=503, detail="Status connection unavailable")
            started = time.perf_counter()
            try:
                info = await client.get_camera_system_info(timeout=0.7, max_retries=0)
                rtt = round((time.perf_counter() - started) * 1000, 1)
                self.rtt_samples.append(rtt)
                self.rtt_failures.append(False)
                self.camera_status = {
                    "gimbal_mode": info.gimbal_motion_mode.name,
                    "recording": info.record_sta.name,
                    "hdr": bool(info.hdr_sta),
                    "mounting": info.gimbal_mounting_dir.name,
                    "video_output": info.video_hdmi_or_cvbs.name,
                }
                self.status_time = time.monotonic()
                self.status_error = None
                return self.camera_status
            except Exception as e:
                self.rtt_samples.append(None)
                self.rtt_failures.append(True)
                self.status_error = str(e) or "Camera status query failed"
                self.last_status_failure = f"{type(e).__name__}: {self.status_error}"
                raise

    def begin_confirmation(self, kind, target, send_ms):
        self.action_counter += 1
        action = {
            "id": self.action_counter, "target": target, "status": "pending",
            "send_ms": send_ms, "confirmation_ms": None, "error": None,
            "_started": time.monotonic(),
        }
        self.actions[kind] = action
        self.confirmation_tasks[kind] = asyncio.create_task(self.confirm_action(kind, action))

    async def confirm_action(self, kind, action):
        field = "gimbal_mode" if kind == "mode" else "recording"
        try:
            # Query immediately; subsequent reads are spaced only when the
            # camera has not applied the command yet. This never holds HTTP open.
            while time.monotonic() - action["_started"] < 3:
                try:
                    info = await self.refresh_status()
                    if info[field] == action["target"]:
                        action["status"] = "confirmed"
                        return
                    if kind == "record" and info[field] in ("NO_TF_CARD", "DATA_LOSS"):
                        action["status"] = "failed"
                        action["error"] = info[field]
                        return
                except Exception:
                    pass
                await asyncio.sleep(0.1)
            action["status"] = "unconfirmed"
            action["error"] = self.status_error or "Camera did not report the requested state within 3 seconds"
        finally:
            action["confirmation_ms"] = round((time.monotonic() - action["_started"]) * 1000, 1)

    async def poll_status(self):
        while True:
            if self.is_connected:
                try:
                    await self.refresh_status()
                except Exception:
                    pass
            await asyncio.sleep(1)

    def status_snapshot(self):
        now = time.monotonic()
        age = now - self.status_time if self.status_time else None
        samples = sorted(sample for sample in self.rtt_samples if sample is not None)
        def percentile(fraction):
            return samples[max(0, math.ceil(len(samples) * fraction) - 1)] if samples else None
        return {
            "camera": self.camera_status,
            "actions": {
                kind: {
                    **{key: value for key, value in action.items() if not key.startswith("_")},
                    "elapsed_ms": round((now - action["_started"]) * 1000, 1),
                }
                for kind, action in self.actions.items()
            },
            "status_age_ms": round(age * 1000) if age is not None else None,
            "zoom": self.zoom,
            "zoom_max": self.zoom_max,
            "lock": self.lock_snapshot(),
            "status_fresh": self.is_connected and age is not None and age < 3 and not self.status_error,
            "status_error": self.status_error,
            "latency": {
                "camera_rtt_ms": self.rtt_samples[-1] if self.rtt_samples else None,
                "camera_p50_ms": percentile(0.5), "camera_p95_ms": percentile(0.95),
                "samples": len(samples),
                "timeouts": sum(self.rtt_failures), "queries": len(self.rtt_failures),
                "last_failure": self.last_status_failure,
                "jpeg_ms": self.jpeg_ms,
                "frame_age_ms": round((now - self.last_frame_time) * 1000) if self.last_frame_time else None,
                "attitude_age_ms": round((now - self.attitude_time) * 1000) if self.attitude_time else None,
            },
        }

    async def send_motion(self, kind, value):
        moving = any(value) if kind == "rotate" else value != 0
        if not self.client or (moving and not self.is_connected):
            raise HTTPException(status_code=503, detail="Camera not connected")
        for _ in range(1 if moving else 3):
            if kind == "rotate":
                await self.client.rotate_nowait(*value)
            else:
                await self.client.manual_zoom_nowait(value)
        if moving:
            self.motion_deadlines[kind] = time.monotonic() + 0.4
        else:
            self.motion_deadlines.pop(kind, None)
            if kind == "zoom" and not (self.zoom_refresh_task and not self.zoom_refresh_task.done()):
                self.zoom_refresh_task = asyncio.create_task(self.refresh_zoom())

    async def _send_lock_rate(self, yaw: int, pitch: int) -> None:
        # The lock sends every frame; a stop goes out (as three packets) only once.
        if (yaw, pitch) == (0, 0) and self.lock_last_command == (0, 0):
            return
        self.lock_last_command = (yaw, pitch)
        try:
            await self.send_motion("rotate", (yaw, pitch))
        except HTTPException:
            pass  # camera offline; the watchdog and reconnect logic handle it

    async def _send_lock_angle(self, yaw: float, pitch: float) -> None:
        if self.client and self.is_connected:
            await self.client.set_attitude_nowait(yaw, pitch)

    def loop_model(self) -> LoopModel:
        cfg = self.pointing
        return LoopModel(
            deg_per_unit=(cfg.deg_per_unit_yaw, cfg.deg_per_unit_pitch),
            command_delay_s=cfg.command_delay_ms / 1000,
            video_delay_s=cfg.frame_delay_ms / 1000,
        )

    async def start_lock(self, x: float, y: float) -> dict:
        async with self.tracking_lock:
            await self._release_lock("Point lock replaced")
            return await self._start_lock(x, y)

    async def _start_lock(self, x: float, y: float) -> dict:
        """Lock the spot at normalized offset (x, y) from the centre of the latest frame."""
        image = self.latest_image
        if image is None or not self.is_connected:
            raise HTTPException(status_code=503, detail="Point lock needs live video from a connected camera")
        if self.calibrating:
            raise HTTPException(status_code=409, detail="Wait for the loop measurement to finish")
        cfg = self.pointing
        height, width = image.shape[:2]
        if cfg.lock_control == "firmware":
            if not self.firmware_tracking_supported():
                self.lock_reason = "Firmware tracking unavailable: requires the inspected camera 0.3.7 / gimbal 0.4.9"
                raise HTTPException(status_code=503, detail=self.lock_reason)
            if time.monotonic() - self.latest_image_time >= 0.4:
                raise HTTPException(status_code=503, detail="Firmware point lock needs a fresh video frame")
            # Stop any outstanding manual speed command and its delayed stop timer.
            await self._firmware_emergency_stop()
            self.motion_deadlines.pop("rotate", None)
            link = self.firmware_link
            if link is not None and not link.ready:
                raise HTTPException(status_code=503, detail=link.error or (
                    "Waiting for the camera's tracking video (it starts at a keyframe, "
                    "about every 2.5 s)"))
            lock = FirmwarePointLock(
                link.client if link is not None
                else FirmwareTrackingClient(self.ip, trace=self.trace_tracking),
                owns_connection=link is None,
                stop=self._firmware_emergency_stop, model=cfg.lock_model,
                # No attitude-based delay correction: with an unreliable pitch sign (inverted
                # bench mount) it drove the gimbal to its limit. Damp with the response instead.
                response=min(cfg.lock_response, 1.0),
            )
            self.point_lock = lock
            self.lock_reason = None
            try:
                await lock.start(image, (x + 0.5) * width, (y + 0.5) * height,
                                 size=max(40, width // 20), timestamp=self.latest_image_time,
                                 zoom=self.zoom or 1.0)
            except Exception as exc:
                self.lock_reason = lock.reason or f"Firmware tracking unavailable: {str(exc) or type(exc).__name__}"
                lock.reason = self.lock_reason
                self.lock_exit_confirmed = lock.exit_confirmed
                raise HTTPException(status_code=503, detail=self.lock_reason) from exc
            return self.lock_snapshot()
        loop = self.loop_model()
        # With attitude available the delay is compensated, so the gain follows the command delay.
        compensated = self.attitude_history.latest() is not None
        gains = LockGains.for_model(loop, compensated=compensated)
        gains.kp *= cfg.lock_response
        gains.ki *= cfg.lock_response ** 2
        gains.max_speed = cfg.lock_max_speed
        lock = GimbalPointLock(
            send=self._send_lock_rate, send_angle=self._send_lock_angle,
            hfov_deg=cfg.hfov_deg, model=cfg.lock_model, loop=loop, gains=gains,
            attitude=self.attitude_history if compensated else None,
            attitude_signs=(cfg.yaw_sign, cfg.pitch_sign),
            control=cfg.lock_control if compensated else "rate",
            trust_video_delay=cfg.calibrated,
        )
        lock.lock(image, (x + 0.5) * width, (y + 0.5) * height, size=max(40, width // 20))
        self.point_lock = lock
        self.lock_reason = None
        self.lock_last_command = None  # always send the first command of a new lock
        return self.lock_snapshot()

    def gimbal_rate(self, window: float = 0.3) -> Optional[tuple[float, float]]:
        """Measured (yaw, pitch) turn rate in deg/s over the last ``window`` seconds."""
        samples = self.attitude_history.samples
        if len(samples) < 2 or time.monotonic() - samples[-1][0] > 0.3:
            return None
        newest = samples[-1]
        oldest = next((s for s in samples if s[0] >= newest[0] - window), samples[0])
        span = newest[0] - oldest[0]
        if span < 0.05:
            return None
        return (newest[1] - oldest[1]) / span, (newest[2] - oldest[2]) / span

    def trace_tracking(self, event: str, fields: dict) -> None:
        self.tracking_log.append({"id": next(self.tracking_log_ids), "t": time.time(),
                                  "event": event, **fields})

    async def _firmware_emergency_stop(self) -> None:
        if self.client:
            await asyncio.wait_for(self.client.rotate_nowait(0, 0), timeout=0.4)

    async def release_lock(self, reason: Optional[str] = None, *, require_confirmed: bool = True) -> None:
        async with self.tracking_lock:
            await self._release_lock(reason, require_confirmed=require_confirmed)

    async def _release_lock(self, reason: Optional[str] = None, *, require_confirmed: bool = True) -> None:
        lock, self.point_lock = self.point_lock, None
        if isinstance(lock, FirmwarePointLock):
            await lock.release(reason or "Point lock released")
            self.lock_reason = lock.reason
            self.lock_exit_confirmed = lock.exit_confirmed
        elif lock is not None and lock.active:
            await lock.release()
            self.lock_reason = reason
            if reason:
                logger.info(reason)
        if require_confirmed and not self.lock_exit_confirmed:
            raise HTTPException(status_code=503, detail=self.lock_reason or "Firmware exit is unconfirmed")

    def lock_snapshot(self) -> dict:
        lock = self.point_lock
        if isinstance(lock, FirmwarePointLock):
            self.lock_reason = lock.reason
            self.lock_exit_confirmed = lock.exit_confirmed
        if lock is None or not lock.active:
            if self.lock_reason or not self.lock_exit_confirmed:
                return {"state": "idle", "reason": self.lock_reason, "exit_confirmed": self.lock_exit_confirmed}
            return {"state": "idle"}
        s = lock.status
        snapshot = {
            "state": s.state.value, "score": round(s.score, 2), "on_screen": s.on_screen,
            "x": round(s.x / s.width - 0.5, 4) if s.width else 0.0,
            "y": round(s.y / s.height - 0.5, 4) if s.height else 0.0,
            "error_deg": [round(v, 2) for v in s.error_deg], "command": list(s.command),
            "update_ms": self.lock_ms, "compensated": s.compensated, "control": lock.control,
            "target_rate_deg_s": [round(v, 1) for v in s.target_rate_deg_s],
        }
        if isinstance(lock, FirmwarePointLock):
            snapshot.update(error_deg=None, command=None, compensated=None, target_rate_deg_s=None)
        return snapshot

    async def calibrate(self) -> dict:
        """Measure turn rate, delays, axis directions and field of view; store them."""
        if not self.client or not self.is_connected or self.latest_image is None:
            raise HTTPException(status_code=503, detail="Measuring needs live video from a connected camera")
        if self.calibrating:
            raise HTTPException(status_code=409, detail="A measurement is already running")
        await self.release_lock("Point lock released for loop measurement")
        client = self.client
        self.calibrating = True
        try:
            result = await calibrate_loop(
                client.rotate_nowait, self.attitude_history, self.calibration_recorder,
                hfov_deg=self.pointing.hfov_deg, zoom=self.zoom or 1.0,
            )
        except CalibrationError as e:
            raise HTTPException(status_code=422, detail=str(e))
        finally:
            self.calibrating = False
            self.calibration_recorder.stop()
            for _ in range(3):
                try:
                    await client.rotate_nowait(0, 0)
                except Exception:
                    pass
        # A failed axis fit gives a garbage sign and delays; keep the previous settings instead.
        axes = [("yaw", result.yaw), ("pitch", result.pitch)]
        bad = [name for name, r in axes if r is not None and (r.fit_error_deg > 1.0 or r.command_delay_s > 1.0)]
        if bad:
            raise HTTPException(status_code=422, detail=(
                f"Measurement failed on {' and '.join(bad)}; settings were not changed. Level the camera "
                "so it can tilt both ways, aim at a detailed, still scene and measure again."))
        cfg = self.pointing
        model = result.model
        cfg.deg_per_unit_yaw, cfg.deg_per_unit_pitch = (round(v, 4) for v in model.deg_per_unit)
        cfg.command_delay_ms = round(model.command_delay_s * 1000, 1)
        cfg.frame_delay_ms = round(model.video_delay_s * 1000, 1)
        cfg.yaw_sign, cfg.pitch_sign = result.attitude_signs
        cfg.hfov_deg = round(result.hfov_deg, 1)
        cfg.calibrated = True
        return {"config": vars(cfg), "notes": result.notes,
                "fit_error_deg": round(result.yaw.fit_error_deg, 2)}

    async def refresh_zoom(self):
        """Read the zoom level and range; pointer math needs the current field of view."""
        client = self.client
        if not client:
            return
        try:
            if self.zoom_max is None:
                self.zoom_max = (await client.get_zoom_range()).max_zoom
            self.zoom = await client.get_current_zoom()
        except Exception as e:
            logger.debug(f"Zoom query failed: {e}")

    async def look(self, req: "LookRequest"):
        """Point the gimbal so the scene at `anchor` appears at `to`, optionally at a new zoom."""
        if not self.client or not self.is_connected:
            raise HTTPException(status_code=503, detail="Camera not connected")
        cfg = self.pointing
        same = req.gesture and self.gesture and self.gesture["id"] == req.gesture
        gesture = self.gesture if same else None
        if gesture is None:
            # Use the pose from when the clicked frame was captured, not the latest one.
            pose = self.attitude_history.at(time.monotonic() - cfg.video_delay_ms / 1000)
            if pose is None:
                raise HTTPException(status_code=503, detail="No gimbal attitude received yet")
            gesture = {
                "id": req.gesture,
                "yaw": pose[0] * cfg.yaw_sign, "pitch": pose[1] * cfg.pitch_sign,
                "zoom": self.zoom or 1.0,
            }
            self.gesture = gesture if req.gesture else None
        zoom = gesture["zoom"]
        if req.zoom is not None:
            zoom = max(1.0, min(round(req.zoom, 1), self.zoom_max or 6.0))
        optics = {"aspect": req.aspect, "hfov_deg": cfg.hfov_deg}
        world = screen_to_world(gesture["yaw"], gesture["pitch"], req.anchor_x, req.anchor_y,
                                zoom=gesture["zoom"], **optics)
        yaw, pitch = center_for(*world, req.to_x, req.to_y, zoom=zoom,
                                pitch_hint=gesture["pitch"], **optics)
        wanted = (yaw * cfg.yaw_sign, pitch * cfg.pitch_sign)
        target = clamp_attitude(*wanted)
        if req.zoom is not None and zoom != self.zoom:
            try:
                await self.client.absolute_zoom(zoom)
                self.zoom = zoom
            except ConfigurationError as e:
                raise HTTPException(status_code=409, detail=str(e))
        await self.client.set_attitude_nowait(*target)
        return {
            "yaw": round(target[0], 1), "pitch": round(target[1], 1), "zoom": self.zoom,
            "zoom_max": self.zoom_max,
            # True when the gimbal's travel limits cut the move short.
            "limited": any(abs(a - b) > 0.05 for a, b in zip(target, wanted, strict=True)),
        }

    async def motion_watchdog(self):
        # Stop if the page disappears or updates stop arriving. UDP delivery is
        # unconfirmed; send three stops without waiting for ACKs.
        while True:
            for kind, deadline in list(self.motion_deadlines.items()):
                if time.monotonic() >= deadline:
                    try:
                        await self.send_motion(kind, (0, 0) if kind == "rotate" else 0)
                    except Exception:
                        pass
                    self.motion_deadlines.pop(kind, None)
            await asyncio.sleep(0.05)

    async def shutdown(self):
        await self.release_lock("Point lock released: camera shutting down", require_confirmed=False)
        link, self.firmware_link = self.firmware_link, None
        if link is not None:
            await link.stop()
        for task in list(self.confirmation_tasks.values()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.confirmation_tasks.clear()
        self.actions.clear()
        self.last_status_failure = None
        for task in (self.status_task, self.motion_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self.status_task = self.motion_task = None
        for kind in list(self.motion_deadlines):
            try:
                await self.send_motion(kind, (0, 0) if kind == "rotate" else 0)
            except Exception:
                pass
        self.motion_deadlines.clear()
        self.camera_status = None
        self.status_time = self.attitude_time = 0.0
        self.attitude_history.clear()
        self.zoom = self.zoom_max = None
        self.gesture = None
        self.latest_image = None
        self.latest_image_time = 0.0
        self.firmware_version = None
        self.lock_ms = None
        self.status_error = None
        self.rtt_samples.clear()
        self.rtt_failures.clear()
        self.jpeg_ms = None
        self.stop_event.set()
        self.connection_error = None
        self.is_connected = False
        self.latest_frame = None
        self.last_frame_time = 0.0
        self.stream_error = None
        self.frame_event.clear()
        if self.watchdog_task:
            self.watchdog_task.cancel()
            try:
                await asyncio.wait_for(self.watchdog_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self.watchdog_task = None
            
        if self.stream:
            try:
                await asyncio.wait_for(self.stream.stop(), timeout=3.0)
            except Exception:
                pass
            self.stream = None
        if self.client:
            try:
                await asyncio.wait_for(self.client.close(), timeout=2.0)
            except Exception:
                pass
            self.client = None
        self.transport = None
        if self.media:
            self.media = None
        logger.info("Clients shut down")

    async def restart_stream(self):
        async with self.lock:
            if self.stream:
                logger.info("Restarting video stream...")
                await self.stream.stop()
                # Use current config to restart
                await asyncio.sleep(1.0) # Small delay
                await self.stream.start()
                logger.info("Video stream restarted")

    async def toggle_stream(self, enabled: bool):
        async with self.lock:
            self.live_enabled = enabled
            if not self.stream:
                return
            if enabled and not self.stream.is_running and self.is_connected:
                logger.info("Starting backend stream...")
                try:
                    await asyncio.wait_for(self.stream.start(), timeout=10.0)
                    self.stream_error = None
                except Exception as e:
                    self.stream_error = str(e) or "Video stream timed out"
                    raise
            elif not enabled and self.stream.is_running:
                await self.release_lock("Point lock released: video stopped", require_confirmed=False)
                logger.info("Stopping backend stream (deep sleep)...")
                await asyncio.wait_for(self.stream.stop(), timeout=5.0)
                self.latest_frame = None
                self.last_frame_time = 0.0

state = CameraState()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load initial config or use default
    await state.initialize(GLOBAL_CONFIG["camera_ip"])
    yield
    await state.shutdown()

app = FastAPI(lifespan=lifespan)

@app.middleware("http")
async def request_timing(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.2f}"
    return response

# Models
class IPConfigRequest(BaseModel):
    ip: IPv4Address

class RotateRequest(BaseModel):
    yaw: int = Field(ge=-100, le=100)
    pitch: int = Field(ge=-100, le=100)

class GimbalModeRequest(BaseModel):
    mode: str

class EncodingRequest(BaseModel):
    # Simplified for UI
    resolution: Optional[str] = None
    bitrate_kbps: Optional[int] = None

class CommandRequest(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)
    confirm: bool = False

class BackendRequest(BaseModel):
    backend: StreamBackend

class LookRequest(BaseModel):
    # Normalized offsets from the image center: -0.5 = left/top edge, 0.5 = right/bottom.
    anchor_x: float = Field(ge=-0.5, le=0.5)
    anchor_y: float = Field(ge=-0.5, le=0.5)
    to_x: float = Field(default=0.0, ge=-0.5, le=0.5)
    to_y: float = Field(default=0.0, ge=-0.5, le=0.5)
    aspect: float = Field(default=16 / 9, gt=0.2, lt=5)
    zoom: Optional[float] = Field(default=None, ge=1.0, le=30.0)
    # Requests sharing a gesture id keep the pose and zoom from its first request.
    gesture: Optional[str] = Field(default=None, max_length=64)

class PointingConfigRequest(BaseModel):
    hfov_deg: float = Field(gt=5, lt=170)
    yaw_sign: Literal[-1, 1]
    pitch_sign: Literal[-1, 1]
    video_delay_ms: float = Field(ge=0, le=3000)
    # Defaults keep settings saved by older dashboards valid.
    lock_control: Literal["angle", "rate", "firmware"] = "angle"
    lock_response: float = Field(default=1.0, gt=0.1, le=5)
    lock_max_speed: int = Field(default=100, ge=5, le=100)
    lock_model: Literal["local", "global"] = "local"
    # Signed: a negative rate means +speed turns the picture left/down.
    deg_per_unit_yaw: float = Field(default=1.0, ge=-20, le=20)
    deg_per_unit_pitch: float = Field(default=1.0, ge=-20, le=20)

    @field_validator("deg_per_unit_yaw", "deg_per_unit_pitch")
    @classmethod
    def _nonzero_rate(cls, value: float) -> float:
        if abs(value) < 0.05:
            raise ValueError("turn rate per unit must be at least 0.05 deg/s in magnitude")
        return value
    command_delay_ms: float = Field(default=60.0, ge=0, le=1000)
    frame_delay_ms: float = Field(default=200.0, ge=0, le=2000)
    calibrated: bool = False

class LockRequest(BaseModel):
    # Normalized offsets from the image centre: -0.5 = left/top edge, 0.5 = right/bottom.
    x: float = Field(ge=-0.5, le=0.5)
    y: float = Field(ge=-0.5, le=0.5)

class ZoomRequest(BaseModel):
    zoom: float = Field(ge=1.0, le=30.0)

# Endpoints
@app.post("/api/config/ip")
async def set_ip(req: IPConfigRequest):
    try:
        ip = str(req.ip)
        await state.initialize(ip)
        GLOBAL_CONFIG["camera_ip"] = ip
        return {"status": "ok", "ip": ip}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/config/ip")
async def get_ip():
    return {
        "ip": GLOBAL_CONFIG["camera_ip"],
        "backend": state.stream_backend.value,
        "connected": state.is_connected,
        "connection_error": state.connection_error,
        "stream_ready": state.is_connected and state.live_enabled and time.monotonic() - state.last_frame_time < 5.0,
        "stream_error": state.stream_error,
        **state.status_snapshot(),
    }

@app.post("/api/gimbal/mode")
async def set_gimbal_mode(req: GimbalModeRequest):
    modes = {"LOCK": CaptureFuncType.LOCK_MODE, "FOLLOW": CaptureFuncType.FOLLOW_MODE, "FPV": CaptureFuncType.FPV_MODE}
    if req.mode not in modes:
        raise HTTPException(status_code=422, detail="Mode must be LOCK, FOLLOW or FPV")
    if not state.client or not state.is_connected:
        raise HTTPException(status_code=503, detail="Camera not connected")
    queued = time.perf_counter()
    async with state.action_lock:
        queue_ms = round((time.perf_counter() - queued) * 1000, 1)
        if state.actions.get("mode", {}).get("status") == "pending":
            raise HTTPException(status_code=409, detail="A mode change is awaiting camera confirmation")
        await state.release_lock("Point lock released by gimbal mode change")
        try:
            started = time.perf_counter()
            await state.client.capture(modes[req.mode])
            send_ms = round((time.perf_counter() - started) * 1000, 1)
        except Exception as e:
            raise HTTPException(status_code=503, detail=str(e))
        # Transmission is not confirmation: only a subsequent camera report
        # is allowed to select the active mode in the UI.
        state.begin_confirmation("mode", req.mode, send_ms)
        return {"status": "sent", "command_timing": {"send_ms": send_ms, "queue_ms": queue_ms}, **state.status_snapshot()}

@app.post("/api/gimbal/rotate")
async def rotate(req: RotateRequest):
    started = time.perf_counter()
    # Manual control takes over from point lock.
    await state.release_lock("Point lock released by manual rotation")
    try:
        await state.send_motion("rotate", (req.yaw, req.pitch))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"status": "sent", "confirmed": False, "command_timing": {"send_ms": round((time.perf_counter() - started) * 1000, 1)}}

@app.post("/api/gimbal/look")
async def look(req: LookRequest):
    started = time.perf_counter()
    await state.release_lock("Point lock released by pointer aiming")
    try:
        result = await state.look(req)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {**result, "command_timing": {"send_ms": round((time.perf_counter() - started) * 1000, 1)}}

@app.get("/api/pointing/config")
async def get_pointing_config():
    return vars(state.pointing)

@app.post("/api/pointing/config")
async def set_pointing_config(req: PointingConfigRequest):
    async with state.tracking_lock:
        if vars(state.pointing) != req.model_dump():
            await state._release_lock("Point lock released: steering settings changed")
        state.pointing = PointingConfig(**req.model_dump())
    await state.sync_firmware_link()
    return vars(state.pointing)

@app.post("/api/track/lock")
async def track_lock(req: LockRequest):
    return await state.start_lock(req.x, req.y)

@app.post("/api/track/calibrate")
async def track_calibrate():
    """Turn the gimbal briefly on each axis to measure the loop (moves the camera)."""
    return await state.calibrate()

@app.post("/api/track/release")
async def track_release():
    await state.release_lock("Point lock released from the dashboard")
    return state.lock_snapshot()

@app.post("/api/camera/zoom_to")
async def zoom_to(req: ZoomRequest):
    """Set an absolute zoom without moving the gimbal (keeps a point lock running)."""
    if not state.client or not state.is_connected:
        raise HTTPException(status_code=503, detail="Camera not connected")
    zoom = max(1.0, min(round(req.zoom, 1), state.zoom_max or 6.0))
    try:
        await state.client.absolute_zoom(zoom)
    except ConfigurationError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
    state.zoom = zoom
    return {"zoom": zoom, "zoom_max": state.zoom_max}

@app.post("/api/gimbal/center")
async def center():
    if not state.client:
        raise HTTPException(status_code=503, detail="Camera not connected")
    await state.release_lock("Point lock released by centering")
    try:
        started = time.perf_counter()
        await state.client.one_key_centering(CenteringAction.CENTER)
    except Exception as e:
        logger.error(f"Center command failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "ok", "command_timing": {"sdk_reply_ms": round((time.perf_counter() - started) * 1000, 1)}}

@app.post("/api/camera/photo")
async def take_photo():
    if not state.client:
        raise HTTPException(status_code=503, detail="Camera not connected")
    try:
        started = time.perf_counter()
        await state.client.capture(CaptureFuncType.PHOTO)
    except Exception as e:
        logger.error(f"Photo command failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "sent", "confirmed": False, "command_timing": {"send_ms": round((time.perf_counter() - started) * 1000, 1)}}

@app.post("/api/camera/record")
async def toggle_record():
    if not state.client or not state.is_connected:
        raise HTTPException(status_code=503, detail="Camera not connected")
    queued = time.perf_counter()
    async with state.action_lock:
        queue_ms = round((time.perf_counter() - queued) * 1000, 1)
        if state.actions.get("record", {}).get("status") == "pending":
            raise HTTPException(status_code=409, detail="Recording is awaiting camera confirmation")
        try:
            started = time.perf_counter()
            before = await state.refresh_status()
            preflight_ms = round((time.perf_counter() - started) * 1000, 1)
            if before["recording"] not in ("RECORDING", "NOT_RECORDING"):
                raise HTTPException(status_code=409, detail="Recording unavailable: " + before["recording"])
            started = time.perf_counter()
            await state.client.capture(CaptureFuncType.START_RECORD)
            send_ms = round((time.perf_counter() - started) * 1000, 1)
            target = "NOT_RECORDING" if before["recording"] == "RECORDING" else "RECORDING"
            state.begin_confirmation("record", target, send_ms)
            return {"status": "sent", "command_timing": {"send_ms": send_ms, "queue_ms": queue_ms, "preflight_ms": preflight_ms}, **state.status_snapshot()}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=503, detail="Recording state unconfirmed: " + str(e))

@app.post("/api/camera/zoom")
async def zoom(direction: int): # -1, 0, 1
    if not state.client:
        raise HTTPException(status_code=503, detail="Camera not connected")
    try:
        started = time.perf_counter()
        if direction not in (-1, 0, 1):
            raise HTTPException(status_code=422, detail="Direction must be -1, 0 or 1")
        await state.send_motion("zoom", direction)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"status": "sent", "confirmed": False, "command_timing": {"send_ms": round((time.perf_counter() - started) * 1000, 1)}}

@app.get("/api/camera/encoding")
async def get_encoding():
    if not state.is_connected or not state.client:
        raise HTTPException(status_code=503, detail="Camera not connected")
    try:
        params = await state.client.get_encoding_params(StreamType.MAIN)
        return {
            "stream_type": params.stream_type.name,
            "enc_type": params.enc_type.name,
            "resolution": f"{params.resolution_w}x{params.resolution_h}",
            "bitrate_kbps": params.bitrate_kbps,
            "frame_rate": params.frame_rate
        }
    except Exception as e:
        logger.warning(f"Failed to get encoding params: {e}")
        raise HTTPException(status_code=503, detail="Failed to communicate with camera")

@app.post("/api/camera/encoding")
async def set_encoding(req: EncodingRequest):
    if not state.client:
        raise HTTPException(status_code=503, detail="Camera not connected")
    
    try:
        # Get current params to preserve other fields
        curr = await state.client.get_encoding_params(StreamType.MAIN)
        
        target_w, target_h = curr.resolution_w, curr.resolution_h
        if req.resolution:
            parts = req.resolution.split('x')
            if len(parts) == 2:
                target_w = int(parts[0])
                target_h = int(parts[1])
        
        logger.info(f"Setting encoding params: {target_w}x{target_h}, {req.bitrate_kbps or curr.bitrate_kbps} kbps")
        from siyi_sdk.models import EncodingParams
        params = EncodingParams(
            stream_type=curr.stream_type,
            enc_type=curr.enc_type,
            resolution_w=target_w,
            resolution_h=target_h,
            bitrate_kbps=req.bitrate_kbps or curr.bitrate_kbps,
            frame_rate=curr.frame_rate
        )
        
        success = await state.client.set_encoding_params(params)
        logger.info(f"Set encoding status: {success}")
        
        if success:
            # Restart stream in background as it might take a moment
            asyncio.create_task(state.restart_stream())
            
        return {"status": "ok" if success else "failed"}
    except Exception as e:
        logger.error(f"Set encoding failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/storage/format")
async def format_sd():
    if not state.is_connected or not state.client:
        raise HTTPException(status_code=503, detail="Camera not connected")
    try:
        success = await state.client.format_sd_card()
        return {"status": "ok" if success else "failed"}
    except TimeoutError as e:
        logger.warning(f"Format SD was not acknowledged: {e}")
        return {"status": "unconfirmed", "warning": "The camera did not acknowledge the format request"}
    except Exception as e:
        logger.error(f"Format SD failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class RebootRequest(BaseModel):
    camera: bool = False
    gimbal: bool = False

@app.post("/api/system/reboot")
async def reboot_system(req: RebootRequest):
    if not state.is_connected or not state.client:
        raise HTTPException(status_code=503, detail="Camera not connected")
    try:
        cam_ok, gim_ok = await state.client.soft_reboot(camera=req.camera, gimbal=req.gimbal)
        return {"status": "ok", "camera": cam_ok, "gimbal": gim_ok}
    except Exception as e:
        logger.error(f"Reboot failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/system/info")
async def get_system_info():
    if not state.is_connected or not state.client:
        raise HTTPException(status_code=503, detail="Camera not connected")
    try:
        hw_id = await state.client.get_hardware_id()
        fw = await state.client.get_firmware_version()
        
        return {
            "camera_type": hw_id.product_id.label,
            "camera_fw": FirmwareVersion.format_word(fw.camera),
            "gimbal_fw": FirmwareVersion.format_word(fw.gimbal),
            "zoom_fw": FirmwareVersion.format_word(fw.zoom)
        }
    except Exception as e:
        logger.warning(f"Failed to get system info: {e}")
        raise HTTPException(status_code=503, detail="Failed to communicate with camera")

@app.get("/api/media/directories")
async def list_dirs(type: int = 0):
    if not state.is_connected or not state.media:
        raise HTTPException(status_code=503, detail="Camera not connected")
    try:
        dirs = await state.media.list_directories(MediaType(type))
        return [{"name": d.name, "path": d.path} for d in dirs]
    except Exception as e:
        logger.error(f"List directories failed: {e}")
        raise HTTPException(status_code=503, detail="Media API unreachable")

@app.get("/api/media/files")
async def list_files(path: str, type: int = 0):
    if not state.is_connected or not state.media:
        raise HTTPException(status_code=503, detail="Camera not connected")
    try:
        files = await state.media.list_files(MediaType(type), path)
        return [{"name": f.name, "url": f.url} for f in files]
    except Exception as e:
        logger.error(f"List files failed: {e}")
        raise HTTPException(status_code=503, detail="Media API unreachable")

@app.get("/api/media/download")
async def download_media(url: str):
    import urllib.request
    import os
    try:
        # Extract filename from URL
        filename = os.path.basename(url.split('?')[0])
        
        # We proxy the download to force 'attachment' disposition
        # so the browser triggers a download dialog instead of playing it.
        def iter_file():
            with urllib.request.urlopen(url, timeout=10) as resp:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
        
        return StreamingResponse(
            iter_file(), 
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename=\"{filename}\""}
        )
    except Exception as e:
        logger.error(f"Download proxy failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/stream/toggle")
async def toggle_stream(enabled: bool):
    try:
        await state.toggle_stream(enabled)
        return {"status": "ok", "enabled": enabled}
    except Exception as e:
        logger.error(f"Toggle stream failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/stream/backend")
async def set_stream_backend(request: BackendRequest):
    async with state.lock:
        state.stream_backend = request.backend
        if state.stream:
            was_running = state.stream.is_running
            if was_running:
                await state.stream.stop()
            state.stream._config.backend = request.backend
            if was_running and state.live_enabled and state.is_connected:
                try:
                    await state.stream.start()
                    state.stream_error = None
                except Exception as exc:
                    state.stream_error = str(exc) or "Video backend unavailable"
                    raise HTTPException(status_code=503, detail=state.stream_error) from exc
    return {"backend": request.backend.value}

@app.get("/api/sdk/commands")
async def list_sdk_commands():
    return [command.describe() for command in COMMANDS.values()]

@app.post("/api/sdk/commands/{name}")
async def run_sdk_command(name: str, request: CommandRequest):
    command = COMMANDS.get(name)
    if command is None:
        raise HTTPException(status_code=404, detail="Unknown A8 Mini command")
    if not state.client or not state.is_connected:
        raise HTTPException(status_code=503, detail="Camera not connected")
    if command.confirmation and not request.confirm:
        raise HTTPException(status_code=409, detail="Confirmation required")
    if name == "heartbeat" and not state.transport.supports_heartbeat:
        raise HTTPException(status_code=422, detail="Heartbeat is only used on TCP connections")
    started_at = time.time()
    started = time.perf_counter()

    def trace(**body):
        # Frames logged while this command ran; concurrent UI traffic may appear too.
        frames = state.transport.between(started_at, time.time()) if state.transport else []
        return {"command": name, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "frames": frames, **body}

    try:
        if not command.read_only:
            await state.release_lock("Point lock released by command explorer")
        result = await execute(state.client, command, request.args)
    except (ValueError, ConfigurationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TimeoutError as exc:
        if name == "format_sd_card":
            return trace(result={"sent": True, "confirmed": False})
        raise HTTPException(status_code=503, detail=trace(error=str(exc), status="timeout")) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=trace(error=str(exc), status="error")) from exc
    return trace(result=result)

@app.get("/api/debug/frames")
async def debug_frames(since: int = 0):
    if not state.transport:
        return {"last_id": 0, "frames": []}
    return {"last_id": state.transport.last_id, "frames": state.transport.since(since)}

@app.get("/api/debug/tracking")
async def debug_tracking(since: int = 0):
    return [record for record in state.tracking_log if record["id"] > since]

@app.websocket("/ws/attitude")
async def websocket_attitude(websocket: WebSocket):
    await websocket.accept()
    try:
        while not state.stop_event.is_set():
            await websocket.send_json({
                **state.attitude, **state.status_snapshot(), "feedback": list(state.feedback),
                "frames_last_id": state.transport.last_id if state.transport else 0,
            })
            await asyncio.sleep(0.1) # 10Hz
    except WebSocketDisconnect:
        pass

async def mjpeg_generator(request: Request) -> AsyncGenerator[bytes, None]:
    while not state.stop_event.is_set():
        if await request.is_disconnected():
            logger.debug("MJPEG client disconnected")
            break
        try:
            # Wait for frame with timeout to prevent hanging on reboot
            await asyncio.wait_for(state.frame_event.wait(), timeout=1.0)
            state.frame_event.clear()
            if state.latest_frame:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + state.latest_frame + b'\r\n')
        except asyncio.TimeoutError:
            # Just loop again and check stop_event and is_disconnected
            continue
        await asyncio.sleep(0.01)

@app.get("/api/stream/video")
async def video_stream(request: Request):
    return StreamingResponse(mjpeg_generator(request), media_type='multipart/x-mixed-replace; boundary=frame')

# Serve Static Files
UI_DIR = os.path.dirname(__file__)
STATIC_DIR = os.path.join(UI_DIR, "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
        return f.read()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=GLOBAL_CONFIG["port"])
