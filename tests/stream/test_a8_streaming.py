"""Bounded storage, terminal notifications, and responsive native shutdown."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import weakref
from unittest.mock import patch

import numpy as np
import pytest

from siyi_sdk.stream import SIYIStream, StreamBackend, StreamConfig, StreamFrame, StreamState
from siyi_sdk.stream._delivery import LatestFrames
from siyi_sdk.stream._h264 import H264Assembler, JitterBuffer, RTPPacket, video_parameters
from siyi_sdk.stream._threaded import ThreadedBackend
from siyi_sdk.stream.gstreamer_backend import copy_bgr_pixels
from siyi_sdk.stream.opencv_backend import OpenCVBackend


def frame(now=0.0, pixels=None):
    pixels = np.zeros((2, 2, 3), dtype=np.uint8) if pixels is None else pixels
    h, w = pixels.shape[:2]
    return StreamFrame(pixels, now, w, h, "test")


class FiniteBackend(ThreadedBackend):
    BACKEND_NAME = "finite"

    def _worker(self):
        self._delivery.publish(frame())


class DelayedBackend(ThreadedBackend):
    BACKEND_NAME = "delayed"

    def _worker(self):
        self._stop_event.wait()
        time.sleep(0.2)


async def test_hour_without_fps_reads_keeps_one_second_of_history():
    stream = SIYIStream(StreamConfig("rtsp://test"))
    image = frame()
    clock = [0.0]
    with patch("siyi_sdk.stream.stream.time.monotonic", side_effect=lambda: clock[0]):
        for index in range(108000):
            clock[0] = index / 30
            stream._record_frame(image)
    assert len(stream._frame_times) <= 31


async def test_blocked_loop_retains_one_latest_image_and_one_notification():
    delivery = LatestFrames()
    images = []
    count = [0]
    original = asyncio.get_running_loop().call_soon_threadsafe

    def counted(*args, **kwargs):
        count[0] += 1
        return original(*args, **kwargs)

    def produce():
        for index in range(30):
            image = np.full((1080, 1920, 3), index, dtype=np.uint8)
            images.append(weakref.ref(image))
            delivery.publish(frame(pixels=image))

    with patch.object(asyncio.get_running_loop(), "call_soon_threadsafe", side_effect=counted):
        worker = threading.Thread(target=produce)
        worker.start()
        worker.join()  # Deliberately stall the event loop while native capture continues.
        assert count[0] == 1
    assert sum(reference() is not None for reference in images) == 1
    latest = await asyncio.wait_for(anext(delivery.frames()), 1)
    assert latest.frame[0, 0, 0] == 29
    delivery.close()


async def test_shutdown_join_keeps_event_loop_responsive():
    backend = DelayedBackend(StreamConfig("rtsp://test"))
    await backend.connect()
    delays = []

    async def timer():
        for _ in range(20):
            started = time.monotonic()
            await asyncio.sleep(0.01)
            delays.append(time.monotonic() - started - 0.01)

    await asyncio.gather(timer(), backend.disconnect())
    # Windows 15.6 ms timer granularity adds noise even without a blocked loop.
    assert sorted(delays)[-2] < (0.03 if sys.platform == "win32" else 0.02)
    assert backend._thread is None


async def test_shutdown_timeout_retains_worker_and_prevents_restart():
    backend = DelayedBackend(StreamConfig("rtsp://test"))

    class Stuck:
        def join(self, timeout):
            pass

        def is_alive(self):
            return True

    worker = Stuck()
    backend._thread = worker
    with pytest.raises(RuntimeError, match="five seconds"):
        await backend.disconnect()
    assert backend._thread is worker and backend.state is StreamState.FAILED
    with pytest.raises(RuntimeError, match="still alive"):
        await backend.connect()


async def test_finite_producer_clears_running_and_allows_restart():
    stream = SIYIStream(StreamConfig("rtsp://test", backend=StreamBackend.OPENCV))
    stream._select_backend = lambda: FiniteBackend(stream._config)
    await stream.start()
    await asyncio.sleep(0.02)
    assert not stream.is_running and stream.state is StreamState.STOPPED
    await stream.start()
    await stream.stop()
    assert stream._backend is None


async def test_concurrent_start_stop_and_callback_self_unsubscribe():
    from .conftest import MockStreamBackend

    stream = SIYIStream(StreamConfig("rtsp://test", backend=StreamBackend.OPENCV))
    stream._select_backend = lambda: MockStreamBackend(stream._config, [frame(), frame()])
    received = []

    def callback(image):
        remove()
        raise RuntimeError("callback")

    remove = stream.on_frame(callback)
    stream.on_frame(received.append)
    await asyncio.gather(stream.start(), stream.start())
    await asyncio.sleep(0)
    await asyncio.gather(stream.stop(), stream.stop())
    assert len(received) == 2
    assert stream.state is StreamState.STOPPED


async def test_auto_requires_first_frame_and_falls_through_failed_candidate():
    from .conftest import MockStreamBackend

    stream = SIYIStream(StreamConfig("rtsp://test", startup_timeout=0.05))
    choices = []

    class NoFrames(MockStreamBackend):
        pass

    def select():
        choices.append(stream._config.backend)
        return (
            NoFrames(stream._config, [])
            if len(choices) == 1
            else MockStreamBackend(stream._config, [frame()])
        )

    stream._select_backend = select
    await stream.start()
    assert choices == [StreamBackend.GSTREAMER, StreamBackend.AIORTSP]
    assert stream._config.backend is StreamBackend.AUTO
    await stream.stop()


@pytest.mark.parametrize("opened", [False, True])
async def test_opencv_limits_apply_to_open_and_read_failures(opened):
    backend = OpenCVBackend(
        StreamConfig("rtsp://test", reconnect_delay=0.001, max_reconnect_attempts=2)
    )

    class Capture:
        def isOpened(self):  # noqa: N802 - OpenCV API spelling
            return opened

        def read(self):
            return False, None

        def release(self):
            pass

    with patch.object(backend, "_open", return_value=Capture()) as open_capture:
        await backend.connect()
        with pytest.raises(OSError):
            await asyncio.wait_for(anext(backend.frame_generator()), 1)
        assert open_capture.call_count == 3
    await backend.disconnect()


@pytest.mark.parametrize("fmt,channels", [("BGR", 3), ("BGRx", 4)])
@pytest.mark.parametrize("width,height", [(3, 2), (1920, 1080)])
def test_gstreamer_visible_pixels_respect_offset_stride_and_ownership(fmt, channels, width, height):
    offset, stride = 16, width * channels + 12
    raw = bytearray(offset + stride * height)
    view = np.ndarray(
        (height, width, channels),
        dtype=np.uint8,
        buffer=raw,
        offset=offset,
        strides=(stride, channels, 1),
    )
    view[:, :, :3] = (10, 20, 30)
    image = copy_bgr_pixels(raw, width, height, fmt, offset, stride)
    view[:] = 0
    assert image.shape == (height, width, 3)
    assert (image == (10, 20, 30)).all()
    assert image.flags.owndata


def test_jitter_reorders_deduplicates_wraps_and_expires_gaps():
    jitter = JitterBuffer()

    def packet(seq):
        return RTPPacket(seq, 0, True, b"\x65data", 96)

    assert jitter.feed(packet(65535), 0)[0][0].seq == 65535
    assert jitter.feed(packet(1), 0.01) == []
    assert jitter.feed(packet(1), 0.02) == []
    assert [p.seq for p, _ in jitter.feed(packet(0), 0.03)] == [0, 1]
    assert jitter.feed(packet(1), 0.04) == []
    assert jitter.feed(packet(3), 0.05) == []
    assert jitter.feed(None, 0.101) == [(packet(3), True)]
    assert not jitter.pending and jitter.bytes == 0


def test_h264_modes_damage_overflow_and_keyframe_recovery():
    assemble = H264Assembler(0)

    def packet(data, ts=0):
        return RTPPacket(0, ts, True, data, 96)

    assert assemble.feed(packet(b"\x65key")) == b"\x00\x00\x00\x01\x65key"
    assert assemble.feed(packet(b"\x7c\x85fragment", 1)) is None
    assert assemble.feed(packet(b"\x41delta", 2)) is None
    assert assemble.feed(packet(b"\x65key", 3)) is not None
    assert assemble.feed(packet(b"\x65" + b"x" * (8 * 1024 * 1024), 4)) is None
    assert not assemble.data
    with pytest.raises(ValueError, match="Interleaved"):
        video_parameters(
            {
                "medias": [
                    {
                        "type": "video",
                        "attributes": {
                            "rtpmap": {"pt": 96, "encoding": "H264", "clockRate": 90000},
                            "fmtp": {"pt": 96, "packetization-mode": "2"},
                        },
                    }
                ]
            }
        )


async def test_rtp_storage_is_bounded_even_when_decoder_cannot_run():
    from types import SimpleNamespace

    from siyi_sdk.stream.aiortsp_backend import AiortspBackend

    backend = AiortspBackend(StreamConfig("rtsp://test"))
    backend._payload_type = 96
    for seq in range(2048):
        backend.handle_rtp(SimpleNamespace(pt=96, seq=seq, ts=0, m=0, p=0, x=0, data=b"x" * 8192))
        assert len(backend._packets) + len(backend._jitter.pending) <= 1024
        assert backend._packet_bytes + backend._jitter.bytes <= 4 * 1024 * 1024
    assert backend._damage


@pytest.mark.parametrize(
    "choice,module_name,class_name",
    [
        (StreamBackend.GSTREAMER, "gstreamer_backend", "GStreamerBackend"),
        (StreamBackend.AIORTSP, "aiortsp_backend", "AiortspBackend"),
        (StreamBackend.OPENCV, "opencv_backend", "OpenCVBackend"),
    ],
)
def test_explicit_backend_selection(monkeypatch, choice, module_name, class_name):
    import importlib

    module = importlib.import_module(f"siyi_sdk.stream.{module_name}")
    if choice is StreamBackend.GSTREAMER:
        monkeypatch.setattr(module, "_GST_AVAILABLE", True)
    stream = SIYIStream(StreamConfig("rtsp://test", backend=choice))
    selected = stream._select_backend()
    assert isinstance(selected, getattr(module, class_name))


@pytest.mark.parametrize(
    "available,expected",
    [
        ((True, True, True), "GStreamerBackend"),
        ((False, True, True), "AiortspBackend"),
        ((False, False, True), "OpenCVBackend"),
        ((False, False, False), None),
    ],
)
def test_auto_selection_follows_available_backends(monkeypatch, available, expected):
    from siyi_sdk.stream import aiortsp_backend, gstreamer_backend, opencv_backend

    for module, enabled in zip(
        (gstreamer_backend, aiortsp_backend, opencv_backend), available, strict=True
    ):
        name = {
            gstreamer_backend: "_GST_AVAILABLE",
            aiortsp_backend: "_AIORTSP_AVAILABLE",
            opencv_backend: "_OPENCV_AVAILABLE",
        }[module]
        monkeypatch.setattr(module, name, enabled)
    stream = SIYIStream(StreamConfig("rtsp://test"))
    if expected is None:
        with pytest.raises(ImportError, match="No streaming backend available"):
            stream._select_backend()
    else:
        assert type(stream._select_backend()).__name__ == expected


async def test_explicit_start_failure_reports_error_and_releases_backend():
    from .conftest import MockStreamBackend

    stream = SIYIStream(
        StreamConfig("rtsp://test", backend=StreamBackend.OPENCV, startup_timeout=0.01)
    )
    backend = MockStreamBackend(stream._config, [])
    stream._select_backend = lambda: backend
    with pytest.raises(TimeoutError):
        await stream.start()
    assert stream.state is StreamState.FAILED
    assert stream.last_error is not None
    assert not backend._connected and stream._backend is None
    await stream.stop()
    assert stream.state is StreamState.STOPPED


async def test_producer_failure_and_reconnect_state_are_visible():
    from .conftest import MockStreamBackend

    stream = SIYIStream(StreamConfig("rtsp://test", backend=StreamBackend.OPENCV))
    fail_now = asyncio.Event()

    class FailsAfterFrame(MockStreamBackend):
        async def frame_generator(self):
            yield frame()
            await fail_now.wait()
            raise OSError("decoder died")

    backend = FailsAfterFrame(stream._config, [])
    stream._select_backend = lambda: backend
    await stream.start()
    backend.state = StreamState.RECONNECTING
    assert stream.state is StreamState.RECONNECTING
    fail_now.set()
    await asyncio.sleep(0.01)
    assert stream.state is StreamState.FAILED
    assert not stream.is_running
    assert isinstance(stream.last_error, OSError)
    await stream.stop()


@pytest.mark.parametrize(
    "bad_payload",
    [
        b"",
        b"\xe5forbidden",
        b"\x78\x00",  # STAP-A with a truncated length
        b"\x78\x00\x05\x65",  # STAP-A with a truncated NAL
        b"\x7c\x85",  # FU-A start, but marker before end
        b"\x7c\x05tail",  # FU-A continuation without start
        b"\x7c\xe5bad",  # Invalid FU-A start/end combination
        b"\x79unsupported",  # Interleaved packetization
    ],
)
def test_damaged_h264_packet_is_dropped_until_next_keyframe(bad_payload):
    assembler = H264Assembler(1)
    assert assembler.feed(RTPPacket(1, 100, True, bad_payload, 96)) is None
    assert assembler.need_keyframe
    assert assembler.feed(RTPPacket(2, 200, True, b"\x41delta", 96)) is None
    assert assembler.feed(RTPPacket(3, 300, True, b"\x65key", 96)) == b"\x00\x00\x00\x01\x65key"


@pytest.mark.parametrize(
    "rtpmap,fmtp,error",
    [
        ({"pt": 96, "encoding": "H265", "clockRate": 90000}, {}, "H.264 only"),
        ({"pt": 96, "encoding": "H264", "clockRate": 8000}, {}, "90000"),
        ({"pt": 96, "encoding": "H264", "clockRate": 90000}, {"pt": 97}, "selected"),
        (
            {"pt": 96, "encoding": "H264", "clockRate": 90000},
            {"pt": 96, "sprop-parameter-sets": "YQ=="},
            "parameter set",
        ),
    ],
)
def test_sdp_rejects_wrong_codec_clock_payload_or_parameter_set(rtpmap, fmtp, error):
    with pytest.raises(ValueError, match=error):
        video_parameters(
            {"medias": [{"type": "video", "attributes": {"rtpmap": rtpmap, "fmtp": fmtp}}]}
        )
