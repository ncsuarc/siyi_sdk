"""Capture times for the camera's bursty 0x90 video stream."""

from __future__ import annotations

import asyncio

import numpy as np

from siyi_sdk.tracking.firmware_link import CaptureClock, FirmwareLink

FPS = 25.0


def test_steady_frames_keep_their_arrival_times():
    clock = CaptureClock()
    for n in range(100):
        arrival = 50.0 + n / FPS + 0.04  # a constant 40 ms in the pipe
        assert clock.update(1000 + n, arrival) == arrival


def test_a_burst_keeps_the_spacing_the_camera_gave_its_frames():
    clock = CaptureClock()
    for n in range(50):  # warm up on frames that arrive when they are made
        clock.update(n, 50.0 + n / FPS + 0.04)
    # Frames 50-59 are held back and all arrive together, with the newest on time.
    arrival = 50.0 + 59 / FPS + 0.04
    captured = [clock.update(n, arrival) for n in range(50, 60)]
    assert np.allclose(captured, [50.0 + n / FPS + 0.04 for n in range(50, 60)], atol=1e-9)
    assert np.allclose(np.diff(captured), 1 / FPS)
    assert captured[-1] == arrival  # the newest frame is not held back at all
    assert captured[0] < arrival - 0.3  # the oldest was, by nine frame intervals


def test_the_clock_follows_drift_between_the_cameras_clock_and_ours():
    clock = CaptureClock()
    fast = 25.025  # the camera's frames actually come 0.1% faster than the nominal rate
    arrival = 0.0
    for n in range(400):  # 16 s, longer than the window
        arrival = 10.0 + n / fast
        captured = clock.update(n, arrival)
    assert abs(captured - arrival) < 0.001


def test_a_counter_restart_starts_the_estimate_afresh_and_time_never_goes_back():
    clock = CaptureClock()
    last = 0.0
    for n in range(75, 150):
        last = clock.update(n, 20.0 + n / FPS)
    # The camera restarts its stream: the counter drops to 0 and the old offsets are meaningless.
    first = clock.update(0, last + 0.5)
    assert first > last
    assert first == last + 0.5
    second = clock.update(1, last + 0.5 + 1 / FPS)
    assert second > first


def test_a_sudden_drop_in_delay_does_not_move_a_timestamp_backwards():
    clock = CaptureClock()
    previous = 0.0
    for n in range(60):
        previous = clock.update(n, 10.0 + n / FPS + 0.5)  # everything 500 ms late
    # A frame suddenly arrives 400 ms sooner than the best seen so far.
    jumped = clock.update(60, 10.0 + 60 / FPS + 0.1)
    assert jumped > previous


async def test_link_delivers_the_newest_frame_with_its_capture_time():
    import time
    from types import SimpleNamespace

    delivered = []
    entered, unblock = asyncio.Event(), asyncio.Event()

    async def sink(image, captured):
        delivered.append((int(image[0, 0]), captured))
        entered.set()
        await unblock.wait()

    link = FirmwareLink("127.0.0.1", on_frame=sink)
    link._decoder = SimpleNamespace(
        decode=lambda batch: (np.full((2, 2), batch[-1][0], np.uint8), batch[-1][3], batch[-1][2])
    )
    workers = [asyncio.create_task(link._decode_loop()), asyncio.create_task(link._consume_loop())]
    now = time.monotonic()
    try:
        link._queue(0, b"x", now)
        await asyncio.wait_for(entered.wait(), 1)
        for i in range(1, 8):
            link._queue(i, b"x", time.monotonic())
            await asyncio.sleep(0.01)

        async def drained():
            while link._packet_count:  # noqa: ASYNC110 - polling a worker-owned counter
                await asyncio.sleep(0.005)

        await asyncio.wait_for(drained(), 1)
        assert link._packet_count == 0  # slow consumer never blocks decoding
        assert link.superseded_frames >= 1
        unblock.set()
        await asyncio.sleep(0.03)
        assert [index for index, _ in delivered] == [0, 7]
        assert delivered[-1][1] > delivered[0][1]
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await link._drain_native()


async def test_compressed_limits_include_inflight_batch_and_invalidate_delivery():
    import threading
    import time
    from types import SimpleNamespace

    entered, unblock = threading.Event(), threading.Event()

    def decode(batch):
        entered.set()
        unblock.wait(2)
        return np.zeros((2, 2), np.uint8), batch[-1][3], batch[-1][2]

    async def sink(*args):
        raise AssertionError("invalidated picture must not be delivered")

    link = FirmwareLink("127.0.0.1", on_frame=sink, max_packet_bytes=4)
    link._decoder = SimpleNamespace(decode=decode)
    task = asyncio.create_task(link._decode_loop())
    try:
        link._queue(0, b"1234", time.monotonic())
        await asyncio.to_thread(entered.wait, 1)
        assert link._packet_bytes == 4
        link._queue(1, b"x", time.monotonic())
        assert isinstance(link._problem, BufferError)
        assert link._latest is None and not link.ready
    finally:
        unblock.set()
        await asyncio.wait_for(task, 1)
        await link._drain_native()


def test_decoder_matches_delayed_output_to_original_timestamp():
    from types import SimpleNamespace

    from siyi_sdk.tracking.firmware_link import _Decoder

    pending = []

    def decode(packet):
        pending.append(packet.pts)
        if len(pending) == 1:
            return []
        return [SimpleNamespace(pts=pending.pop(0), to_ndarray=lambda **kw: np.zeros((2, 2)))]

    decoder = _Decoder.__new__(_Decoder)
    decoder.codec = SimpleNamespace(decode=decode)
    decoder.sequence, decoder.stamp_limit = 0, 256
    decoder.stamps, decoder.image_format = {}, "gray"
    assert decoder.decode([(1, b"x", 20, 10)]) is None
    picture = decoder.decode([(2, b"y", 30, 11)])
    assert picture[1] == 10


async def test_cancelled_native_job_is_retained_until_it_finishes():
    import threading
    import time
    from types import SimpleNamespace

    import pytest

    entered, unblock = threading.Event(), threading.Event()

    def decode(batch):
        entered.set()
        unblock.wait(3)

    async def sink(*args):
        pass

    link = FirmwareLink("127.0.0.1", on_frame=sink)
    link._decoder = SimpleNamespace(decode=decode)
    task = asyncio.create_task(link._decode_loop())
    try:
        link._queue(0, b"x", time.monotonic())
        await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not await link._drain_native()
        with pytest.raises(RuntimeError, match="still running"):
            link.start()
    finally:
        unblock.set()
        assert await link._drain_native()


async def test_overload_notifies_owner_and_reconnects_without_old_frames(monkeypatch):
    import time
    from types import SimpleNamespace

    import siyi_sdk.tracking.firmware_link as module

    released, reconnected = asyncio.Event(), asyncio.Event()
    connections = []
    pictures = []

    class Client:
        def __init__(self, ip, *, trace, on_video):
            self.sink = on_video
            self.is_connected = False
            connections.append(self)

        async def connect(self):
            self.is_connected = True

        async def stop_video(self):
            pass

        async def get_mode(self):
            return False

        async def set_mode(self, enabled):
            assert enabled is False

        async def start_video(self):
            if len(connections) == 1:
                self.sink(0, b"over limit", time.monotonic())
            else:
                reconnected.set()

        async def close(self):
            self.is_connected = False

    async def sink(*picture):
        pictures.append(picture)

    async def failure(reason):
        assert "buffer limit" in reason
        released.set()

    monkeypatch.setattr(module, "FirmwareTrackingClient", Client)
    monkeypatch.setattr(
        module, "_Decoder", lambda *args: SimpleNamespace(decode=lambda batch: None)
    )
    monkeypatch.setattr(module, "RETRY_DELAY", 0.001)
    link = FirmwareLink("127.0.0.1", on_frame=sink, on_failure=failure, max_packet_bytes=4)
    link.start()
    try:
        await asyncio.wait_for(released.wait(), 2)
        await asyncio.wait_for(reconnected.wait(), 2)
        assert not pictures
        assert not connections[0].is_connected
    finally:
        await link.stop()


async def test_stale_arrival_invalidates_pending_picture():
    import time

    async def sink(*args):
        pass

    link = FirmwareLink("127.0.0.1", on_frame=sink)
    link._latest = (np.zeros((2, 2)), time.monotonic())
    link._queue(1, b"x", time.monotonic() - 1)
    assert isinstance(link._problem, TimeoutError)
    assert link._latest is None


async def test_picture_that_aged_while_consumer_was_busy_is_not_delivered():
    import time

    delivered = []

    async def sink(*args):
        delivered.append(args)

    link = FirmwareLink("127.0.0.1", on_frame=sink)
    link._latest = (np.zeros((2, 2)), time.monotonic() - 1, time.monotonic() - 1)
    link._frame_wake.set()
    await link._consume_loop()
    assert not delivered
    assert isinstance(link._problem, TimeoutError)


def test_real_hevc_decoding_preserves_dimensions_and_packet_timestamps():
    import pytest

    pytest.importorskip("av")
    from scripts.performance_workloads import encoded_fixture, scene_frames
    from siyi_sdk.tracking.firmware_link import _Decoder

    frames, _ = scene_frames()
    packets = encoded_fixture(frames[:3])
    decoder = _Decoder("gray", 1, 256)
    results = [decoder.decode([(i, data, 20 + i, 10 + i)]) for i, data in enumerate(packets)]
    assert [picture[1:] for picture in results] == [(10 + i, 20 + i) for i in range(3)]
    assert all(picture[0].shape == (720, 1280) for picture in results)
