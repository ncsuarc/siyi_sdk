# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""Bounded OpenCV capture with native timeouts and supervised retries."""

from __future__ import annotations

import os
import threading
import time
from typing import cast

import numpy as np
import structlog
from numpy.typing import NDArray

try:
    import cv2

    _OPENCV_AVAILABLE = True
except ImportError:
    _OPENCV_AVAILABLE = False

from ._threaded import ThreadedBackend
from .models import StreamConfig, StreamFrame, StreamState

_log = structlog.get_logger(__name__)
_OPEN_LOCK = threading.Lock()  # FFmpeg options are process-wide in OpenCV.


class OpenCVBackend(ThreadedBackend):
    """Capture and reconnect in one owned native worker."""

    BACKEND_NAME = "opencv"

    def __init__(self, config: StreamConfig) -> None:
        """Configure capture; require the optional OpenCV dependency."""
        if not _OPENCV_AVAILABLE:
            raise ImportError("Install opencv-python for this backend")
        super().__init__(config)

    def _open(self) -> cv2.VideoCapture:
        with _OPEN_LOCK:
            key = "OPENCV_FFMPEG_CAPTURE_OPTIONS"
            previous = os.environ.get(key)
            os.environ[key] = (
                f"rtsp_transport;{self._config.transport}|fflags;nobuffer|flags;low_delay|max_delay;0"
            )
            try:
                params: list[int] = []
                for name in ("CAP_PROP_OPEN_TIMEOUT_MSEC", "CAP_PROP_READ_TIMEOUT_MSEC"):
                    if hasattr(cv2, name):
                        params.extend((getattr(cv2, name), 3000))
                try:
                    cap = cv2.VideoCapture(self._config.rtsp_url, cv2.CAP_FFMPEG, params)
                except (TypeError, cv2.error):
                    _log.warning("opencv_native_timeouts_unavailable")
                    cap = cv2.VideoCapture(self._config.rtsp_url, cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, self._config.buffer_size)
                return cap
            finally:
                if previous is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = previous

    def _worker(self) -> None:
        attempts = 0
        delay = self._config.reconnect_delay
        while not self._stop_event.is_set():
            cap = None
            healthy_since = None
            error: Exception = OSError("OpenCV could not open stream")
            try:
                cap = self._open()
                if cap.isOpened():
                    while not self._stop_event.is_set():
                        ok, img = cap.read()
                        if not ok or img is None:
                            error = OSError("OpenCV stream read failed")
                            break
                        now = time.monotonic()
                        if healthy_since is None:
                            healthy_since = now
                        if now - healthy_since >= 30.0:
                            attempts, delay = 0, self._config.reconnect_delay
                        self.state = StreamState.RUNNING
                        h, w = img.shape[:2]
                        if self._delivery:
                            self._delivery.publish(
                                StreamFrame(
                                    cast(NDArray[np.uint8], img), now, w, h, self.BACKEND_NAME
                                )
                            )
            except Exception as exc:
                error = exc
            finally:
                if cap is not None:
                    cap.release()
            if self._stop_event.is_set():
                return
            if (
                self._config.max_reconnect_attempts
                and attempts >= self._config.max_reconnect_attempts
            ):
                raise error
            self.state = StreamState.RECONNECTING
            if self._stop_event.wait(delay):
                return
            attempts += 1
            delay = min(delay * 2, 30.0)
