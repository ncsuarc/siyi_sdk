# Copyright (c) 2026 Mohamed Abdelkader <mohamedashraf123@gmail.com>
# All rights reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""GStreamer RTSP backend using appsink — lowest latency, hardware acceleration.

The GStreamer pipeline decodes H.264 or H.265, converts to BGR, and feeds
frames into an appsink. A GLib.MainLoop runs in a daemon thread; the
new-sample signal handler posts frames to the asyncio event loop.
"""

from __future__ import annotations

import os
import time
from typing import Final, Literal, cast

import numpy as np
import structlog
from numpy.typing import NDArray

# Jetson detection: /etc/nv_tegra_release exists only on L4T (Jetson) systems.
_IS_JETSON: Final[bool] = os.path.exists("/etc/nv_tegra_release")

try:
    import gi

    gi.require_version("Gst", "1.0")
    gi.require_version("GstVideo", "1.0")
    from gi.repository import Gst, GstVideo

    Gst.init(None)
    _GST_AVAILABLE = True
except Exception:  # gi not installed or version unavailable
    _GST_AVAILABLE = False

from ._threaded import ThreadedBackend  # noqa: E402
from .models import StreamConfig, StreamFrame, StreamState  # noqa: E402

_log: Final = structlog.get_logger(__name__)

_RECONNECT_DELAY_CAP: Final[float] = 30.0

# No decoded frame for this long while the pipeline claims to be PLAYING means
# the stream has silently stalled. rtspsrc does not always post EOS when RTP
# simply stops arriving (common on a lossy link), so a timeout is the only
# reliable way to notice. Must exceed the largest expected inter-frame gap.
_STALL_TIMEOUT: Final[float] = 5.0

# Streaming healthily for this long resets the reconnect back-off, so an
# outage hours into a flight starts retrying fast rather than at the cap.
_HEALTHY_RESET_AFTER: Final[float] = 30.0

# Desktop / generic pipeline. decodebin auto-negotiates H.264/H.265 from the
# RTSP SDP, videoconvert produces BGR on the CPU. Used on non-Jetson hosts.
_AUTO_PIPELINE = (
    "rtspsrc location={url} protocols={proto} latency={latency} buffer-mode=slave "
    "! decodebin "
    "! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream "
    "! videoconvert "
    "! video/x-raw,format=BGR "
    "! appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
)

# Jetson pipeline. Uses the NVIDIA hardware decoder (nvv4l2decoder) with DPB
# disabled for low latency, and nvvidconv to convert NV12→BGRx in hardware.
# CPU videoconvert is intentionally omitted — the appsink receives BGRx and
# the SDK strips the alpha channel in NumPy (fast view+copy). Saves a full
# per-frame CPU conversion that otherwise stalls on Orin-class devices.
_JETSON_PIPELINE = (
    "rtspsrc location={url} protocols={proto} latency={latency} buffer-mode=slave "
    "! rtp{codec}depay ! {codec}parse "
    "! nvv4l2decoder disable-dpb=true enable-max-performance=1 "
    "! nvvidconv ! video/x-raw,format=BGRx "
    "! queue max-size-buffers=1 leaky=downstream "
    "! appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
)


class GStreamerBackend(ThreadedBackend):
    """GStreamer + appsink RTSP backend.

    Frames are extracted in the new-sample signal handler and dispatched to
    the asyncio queue. Bus messages are polled in a plain daemon thread instead
    of a GLib.MainLoop, keeping the GLib lock free for GTK3/cv2 in the main
    thread.

    Args:
        config: Stream configuration.
        codec: Codec hint; "h264" or "h265".

    Raises:
        ImportError: If PyGObject / GStreamer is not installed.
    """

    BACKEND_NAME = "gstreamer"

    def __init__(
        self,
        config: StreamConfig,
        codec: Literal["h264", "h265"] = "h264",
    ) -> None:
        """Initialise the GStreamer backend.

        Args:
            config: Stream configuration.
            codec: Codec pipeline to use; "h264" or "h265".

        Raises:
            ImportError: If PyGObject is not available.
        """
        if not _GST_AVAILABLE:
            raise ImportError(
                "PyGObject and GStreamer are required for GStreamerBackend. "
                "Install system packages: gstreamer1.0-plugins-good gstreamer1.0-plugins-bad "
                "python3-gi, then: pip install PyGObject"
            )
        super().__init__(config)
        self._codec = codec
        self._pipeline: Gst.Pipeline | None = None
        self._last_frame_time = 0.0
        self._healthy_since: float | None = None
        self._reconnect_count = 0

    def _build_pipeline_str(self) -> str:
        """Build the GStreamer pipeline string from configuration.

        Resolution order:
          1. ``StreamConfig.pipeline`` override (verbatim, with {url} substituted).
          2. Jetson-tuned pipeline when running on L4T.
          3. Generic decodebin pipeline elsewhere.

        Returns:
            Pipeline description string suitable for gst_parse_launch.
        """
        if self._config.pipeline is not None:
            return self._config.pipeline.format(url=self._config.rtsp_url)

        proto = "tcp" if self._config.transport == "tcp" else "udp"
        template = _JETSON_PIPELINE if _IS_JETSON else _AUTO_PIPELINE
        return template.format(
            url=self._config.rtsp_url,
            proto=proto,
            latency=self._config.latency_ms,
            codec=self._codec,
        )

    def _start_pipeline(self) -> None:
        """Build the pipeline and set it PLAYING. Safe to call repeatedly.

        Raises:
            GLib.Error: If the pipeline description fails to parse.
        """
        pipeline_str = self._build_pipeline_str()
        _log.info("gstreamer_pipeline", pipeline=pipeline_str)

        self._pipeline = Gst.parse_launch(pipeline_str)
        sink = self._pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        if sink is None:
            raise ValueError("Pipeline must contain appsink named sink")
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise OSError("GStreamer could not start pipeline")
        # Treat start as "just saw a frame" so the stall detector gives the
        # pipeline a full _STALL_TIMEOUT to produce its first frame.
        self._last_frame_time = time.monotonic()

    def _stop_pipeline(self) -> None:
        """Tear the current pipeline down to NULL and drop the reference."""
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None

    @property
    def reconnect_count(self) -> int:
        """Number of retries since the last healthy interval."""
        return self._reconnect_count

    def _healthy_reset_due(self, now: float) -> bool:
        return self._healthy_since is not None and now - self._healthy_since >= _HEALTHY_RESET_AFTER

    def _worker(self) -> None:
        self._reconnect_count = 0
        delay = self._config.reconnect_delay
        try:
            while not self._stop_event.is_set():
                reason = "Pipeline failed"
                self._healthy_since = None
                try:
                    self._start_pipeline()
                    while not self._stop_event.is_set():
                        assert self._pipeline is not None
                        msg = self._pipeline.get_bus().timed_pop_filtered(
                            Gst.MSECOND * 100, Gst.MessageType.ERROR | Gst.MessageType.EOS
                        )
                        now = time.monotonic()
                        if msg is not None or now - self._last_frame_time >= _STALL_TIMEOUT:
                            reason = (
                                str(msg.parse_error()[0])
                                if msg is not None and msg.type == Gst.MessageType.ERROR
                                else "End of stream or five-second frame stall"
                            )
                            break
                        if self._healthy_reset_due(now):
                            delay = self._config.reconnect_delay
                            self._reconnect_count = 0
                except Exception as exc:
                    reason = str(exc)
                finally:
                    self._stop_pipeline()
                if self._stop_event.is_set():
                    return
                if (
                    self._config.max_reconnect_attempts
                    and self._reconnect_count >= self._config.max_reconnect_attempts
                ):
                    raise OSError(reason)
                self.state = StreamState.RECONNECTING
                if self._stop_event.wait(delay):
                    return
                self._reconnect_count += 1
                delay = min(delay * 2, _RECONNECT_DELAY_CAP)
        finally:
            self._stop_pipeline()

    def _on_sample(self, sink: object) -> object:
        """GStreamer appsink new-sample signal handler.

        Extracts the video buffer, converts to numpy BGR array, and dispatches
        to the asyncio queue via call_soon_threadsafe.

        Args:
            sink: The appsink element that emitted the signal.

        Returns:
            GLib flow return constant.
        """
        # Cast sink from object to GStreamer appsink type (Gst is Any via ignore_missing_imports).
        gst_sink = cast("Gst.Element", sink)
        try:
            sample = gst_sink.emit("pull-sample")
            buf = sample.get_buffer()
            caps = sample.get_caps()
            structure = caps.get_structure(0)
            width: int = structure.get_value("width")
            height: int = structure.get_value("height")
            fmt: str = structure.get_value("format") or "BGR"

            ok, map_info = buf.map(Gst.MapFlags.READ)
            if not ok:
                return Gst.FlowReturn.OK

            try:
                meta = GstVideo.buffer_get_video_meta(buf)
                if meta is None:
                    if hasattr(GstVideo.VideoInfo, "new_from_caps"):
                        meta = GstVideo.VideoInfo.new_from_caps(caps)
                    else:
                        meta = GstVideo.VideoInfo()
                        if not meta.from_caps(caps):
                            raise ValueError("Could not parse video layout")
                img = copy_bgr_pixels(
                    map_info.data, width, height, fmt, meta.offset[0], meta.stride[0]
                )
            finally:
                buf.unmap(map_info)

            sf = StreamFrame(
                frame=img,
                timestamp=time.monotonic(),
                width=width,
                height=height,
                backend=self.BACKEND_NAME,
            )
            self.state = StreamState.RUNNING
            if self._healthy_since is None:
                self._healthy_since = sf.timestamp
            if self._delivery:
                self._delivery.publish(sf)
            # Feeds the supervisor's stall detector.
            self._last_frame_time = sf.timestamp

            _log.debug(
                "gst_frame_decoded",
                backend=self.BACKEND_NAME,
                width=width,
                height=height,
                timestamp=sf.timestamp,
            )

        except Exception as exc:
            _log.error("gst_sample_error", exc=type(exc).__name__, msg=str(exc))

        return Gst.FlowReturn.OK


def copy_bgr_pixels(
    data: bytes | bytearray | memoryview,
    width: int,
    height: int,
    fmt: str,
    offset: int,
    stride: int,
) -> NDArray[np.uint8]:
    """Copy visible packed pixels using the mapped plane's actual layout."""
    if fmt not in ("BGR", "BGRx"):
        raise ValueError(f"Unsupported appsink format: {fmt}")
    channels = 4 if fmt == "BGRx" else 3
    if width <= 0 or height <= 0 or abs(stride) < width * channels:
        raise ValueError("Invalid video dimensions or row stride")
    raw: NDArray[np.uint8] = np.ndarray(
        (height, width, channels),
        dtype=np.uint8,
        buffer=data,
        offset=offset,
        strides=(stride, channels, 1),
    )
    return raw[:, :, :3].copy()
