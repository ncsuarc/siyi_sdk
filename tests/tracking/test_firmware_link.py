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
    delivered = []
    events = []

    async def sink(image, captured):
        delivered.append((image.shape, captured))

    link = FirmwareLink(
        "127.0.0.1", on_frame=sink, trace=lambda event, fields: events.append((event, fields))
    )
    link._decode = lambda batch: np.zeros((720, 1280, 3), np.uint8)
    task = asyncio.create_task(link._decode_loop())
    try:
        for n in range(30):  # on time
            link._queue(n, b"x", 50.0 + n / FPS + 0.04)
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        delivered.clear()
        events.clear()
        arrival = 50.0 + 39 / FPS + 0.04  # frames 30-39 arrive as one burst
        for n in range(30, 40):
            link._queue(n, b"x", arrival)
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
    assert len(delivered) == 1  # one picture per burst
    shape, captured = delivered[0]
    assert shape == (720, 1280, 3)
    assert captured == arrival  # the newest frame's capture time, not an older one's
    packets = [fields for event, fields in events if event == "video_delivered"]
    assert packets[0]["packets"] == 10 and packets[0]["first"] == 30 and packets[0]["last"] == 39
    assert packets[0]["lag_ms"] == 0.0
