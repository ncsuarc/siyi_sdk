"""Deterministic performance workloads for benchmark_sdk --performance (offline only)."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import math
import os
import platform
import statistics
import time
import tracemalloc
from collections import deque
from fractions import Fraction
from pathlib import Path

import av
import cv2
import numpy as np


def stats(values):
    return {
        "median": float(np.median(values)),
        "p95": float(np.percentile(values, 95)),
        "p99": float(np.percentile(values, 99)),
        "max": float(max(values)),
    }


def timed(fn, count=100):
    for _ in range(10):
        fn()
    runs = []
    for _ in range(5):
        samples = []
        for _ in range(count):
            began = time.perf_counter_ns()
            fn()
            samples.append((time.perf_counter_ns() - began) / 1000)
        runs.append(stats(samples))
    return {
        "unit": "us",
        "runs": runs,
        "median": {k: statistics.median(r[k] for r in runs) for k in runs[0]},
    }


def scene_frames():
    rng = np.random.default_rng(771)
    ground = cv2.GaussianBlur(rng.integers(0, 256, (900, 1600, 3), dtype=np.uint8), (0, 0), 2.5)
    frames = []
    truths = []
    for i in range(25):
        matrix = cv2.getRotationMatrix2D((800, 450), i * 0.12, 1 + i * 0.001)
        matrix[:, 2] += [-160 - i * 2, -90 - i]
        frames.append(cv2.warpAffine(ground, matrix, (1280, 720)))
        truths.append(matrix @ np.array([800, 450, 1]))
    return frames, truths


def encoded_fixture(frames):
    codec = av.CodecContext.create("libx265", "w")
    codec.width, codec.height = 1280, 720
    codec.pix_fmt = "yuv420p"
    codec.time_base = Fraction(1, 25)
    codec.options = {
        "preset": "ultrafast",
        "tune": "zerolatency",
        "x265-params": "log-level=error:pools=none:frame-threads=1:keyint=25:bframes=0",
    }
    packets = []
    for i, img in enumerate(frames):
        f = av.VideoFrame.from_ndarray(img, format="bgr24")
        f.pts = i
        packets.extend(bytes(p) for p in codec.encode(f))
    packets.extend(bytes(p) for p in codec.encode(None))
    return packets


def tracking_trial(PointLock, frames, truths, fmt="bgr24", profile="pan"):
    cv2.setRNGSeed(771)
    tracker = PointLock()
    prepared = []
    for i, img in enumerate(frames):
        source = img.copy()
        if profile == "occlusion" and 8 <= i < 13:
            source[180:540, 400:880] = 128
        elif profile == "low_texture":
            source = cv2.GaussianBlur(source, (0, 0), 8)
        elif profile == "foreground":
            cv2.rectangle(source, (i * 20, 200), (i * 20 + 200, 450), (240, 20, 10), -1)
        elif profile == "loss" and i >= 15:
            source[:] = 128
        decoded = av.VideoFrame.from_ndarray(source, format="bgr24").reformat(format="yuv420p")
        prepared.append(decoded.to_ndarray(format=fmt))
    if profile == "skips":
        prepared, truths = prepared[::3], truths[::3]
    tracker.init(prepared[0], (610, 330, 60, 60))
    errors, times, scores, states = [], [], [], []
    for frame, truth in zip(prepared[1:], truths[1:]):
        began = time.perf_counter_ns()
        ok, _ = tracker.update(frame)
        times.append((time.perf_counter_ns() - began) / 1000)
        errors.append(math.dist(tracker.center(), truth))
        scores.append(tracker.score)
        states.append(bool(ok))
    return {
        "timing_us": stats(times),
        "rms_px": float(np.sqrt(np.mean(np.square(errors)))),
        "max_px": max(errors),
        "lost": states.count(False),
        "states": states,
        "min_score": min(scores),
    }


async def sleep_until(deadline):
    # Windows asyncio can wake timers within its coarse monotonic clock resolution.
    # Pace the producer on perf_counter so worker wakeups do not change the input rate.
    while (remaining := deadline - time.perf_counter()) > 0:
        await asyncio.sleep(remaining)


async def scheduler_trial(GimbalPointLock, history, frames, combined=False, packets=None):
    calls = []
    stopped = asyncio.Event()

    async def send(*args):
        calls.append(time.perf_counter())
        if not combined:
            await asyncio.sleep(0.005)

    before = time.monotonic()
    for t in np.linspace(before - 3, before, 151):
        history.add(t, math.sin(t * 4) * 3, math.cos(t * 3))
    lock = GimbalPointLock(send=send, attitude=history)
    lock.lock(frames[0], 640, 360)
    if not combined:

        async def step(now, dt):
            await send()

        lock._control_step = step
    step_times = []
    original_step = lock._control_step

    async def counted_step(now, dt):
        step_times.append(time.perf_counter())
        await original_step(now, dt)

    lock._control_step = counted_step
    ticks = []

    async def heartbeat():
        deadline = time.monotonic() + 0.01
        while not stopped.is_set():
            await asyncio.sleep(max(0, deadline - time.monotonic()))
            now = time.monotonic()
            ticks.append(max(0, now - deadline) * 1000)
            deadline = now + 0.01

    decode_times, tracking_times = [], []

    async def attitude_feed():
        deadline = time.perf_counter()
        while not stopped.is_set():
            now = time.monotonic()
            history.add(now, math.sin(now * 4) * 3, math.cos(now * 3))
            deadline += 0.02
            await sleep_until(deadline)

    async def workload():
        from siyi_sdk.tracking.firmware_link import FirmwareLink
        import siyi_sdk.tracking.firmware_link as link_module

        async def sink(*args):
            pass

        i = 0
        deadline = time.perf_counter()
        while not stopped.is_set():
            now = time.monotonic()
            if i % len(packets) == 0:
                if hasattr(link_module, "_Decoder"):
                    decoder = link_module._Decoder(
                        FirmwareLink("127.0.0.1", on_frame=sink).image_format, 0, 256
                    )
                    decode = decoder.decode
                else:
                    link = FirmwareLink("127.0.0.1", on_frame=sink)
                    link._decoder = av.CodecContext.create("hevc", "r")
                    link._decoder.thread_type = "SLICE"
                    decode = link._decode
            began = time.perf_counter_ns()
            job = asyncio.create_task(
                asyncio.to_thread(decode, [(i, packets[i % len(packets)], now, now)])
            )
            try:
                output = await asyncio.shield(job)
            except asyncio.CancelledError:
                await job
                raise
            decode_times.append((time.perf_counter_ns() - began) / 1000)
            image = output[0] if isinstance(output, tuple) else output
            if image is not None:
                began = time.perf_counter_ns()
                await lock.update(image, timestamp=now)
                tracking_times.append((time.perf_counter_ns() - began) / 1000)
            i += 1
            deadline += 0.04
            await sleep_until(deadline)

    lock._ensure_loop()
    workers = [asyncio.create_task(heartbeat())]
    if combined:
        workers.extend([asyncio.create_task(workload()), asyncio.create_task(attitude_feed())])
    began = time.perf_counter()
    cpu = time.process_time()
    await asyncio.sleep(1.5)
    duration = time.perf_counter() - began
    cpu = time.process_time() - cpu
    stopped.set()
    for task in workers:
        task.cancel()
    await asyncio.gather(*workers, return_exceptions=True)
    # Drain tracking native work before another trial, where applicable.
    await lock.release()
    intervals = np.diff(calls[:-1]) * 1000
    return {
        "frequency_hz": max(0, len(calls) - 2) / duration,
        "control_hz": len(step_times) / duration,
        "control_interval_ms": stats(np.diff(step_times) * 1000),
        "frames_processed": len(tracking_times),
        "interval_ms": stats(intervals),
        "loop_lag_ms": stats(ticks),
        "cpu_s": cpu,
        "decode_prepare_us": stats(decode_times) if decode_times else None,
        "tracking_us": stats(tracking_times) if tracking_times else None,
    }


async def overload_trial():
    from siyi_sdk.tracking.firmware_link import FirmwareLink
    from types import SimpleNamespace

    delivered = []
    native = []

    async def sink(image, stamp):
        delivered.append((int(image[0, 0]), (time.monotonic() - stamp) * 1000))
        await asyncio.sleep(0.12)

    link = FirmwareLink("127.0.0.1", on_frame=sink)
    modern = hasattr(link, "_consume_loop")

    def decode(batch):
        native.extend(p[0] for p in batch)
        picture = np.full((32, 32), batch[-1][0], np.uint8)
        return (picture, batch[-1][3], batch[-1][2]) if modern else picture

    if modern:
        link._decoder = SimpleNamespace(decode=decode)
    else:
        link._decode = decode
    workers = [asyncio.create_task(link._decode_loop())]
    if modern:
        workers.append(asyncio.create_task(link._consume_loop()))
    high = 0
    try:
        deadline = time.perf_counter()
        for i in range(30):
            link._queue(i, b"x" * 5000, time.monotonic())
            high = max(high, len(link._packets))
            deadline += 0.04
            await sleep_until(deadline)
        began = time.monotonic()
        while (not delivered or delivered[-1][0] != 29) and time.monotonic() - began < 1:
            await asyncio.sleep(0.01)
        return {
            "max_waiting_packets": high,
            "decoded": len(native),
            "delivered": len(delivered),
            "last_index": delivered[-1][0],
            "delivery_age_ms": stats([d[1] for d in delivered]),
            "recovery_ms": (time.monotonic() - began) * 1000,
            "peak_compressed_bytes": getattr(link, "peak_packet_bytes", None),
            "superseded_frames": getattr(link, "superseded_frames", None),
        }
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        if modern:
            await link._drain_native()


async def raw_backlog_trial():
    from siyi_sdk.transport.threaded_tcp import ThreadedTCPTransport

    payload = bytes(range(256)) * 32768
    handlers = []

    async def peer(reader, writer):
        handlers.append(asyncio.current_task())
        try:
            writer.write(payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    transport = ThreadedTCPTransport("127.0.0.1", server.sockets[0].getsockname()[1])
    try:
        await transport.connect()
        await asyncio.sleep(0.01)
        time.sleep(0.15)  # deliberately stall the event loop while the reader thread runs
        await asyncio.sleep(0)  # release coalesced notifications, not the queued data
        queue = transport._queue
        if hasattr(queue, "nbytes"):
            size, count = queue.nbytes, len(queue.items)
        else:
            chunks = [x for x in queue._queue if isinstance(x, tuple)]
            size, count = sum(len(x[0]) for x in chunks), len(chunks)
        digest = hashlib.sha256()
        received = 0
        async for data in transport.stream():
            digest.update(data)
            received += len(data)
        assert received == len(payload) and digest.digest() == hashlib.sha256(payload).digest()
        return {"buffered_bytes": size, "buffered_chunks": count, "received_bytes": received}
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers, return_exceptions=True)


def motor_batch_candidate(estimator):
    current = estimator.dead_time_s
    low = estimator.calibrated_dead_time_s * estimator.dead_time_limits[0]
    high = estimator.calibrated_dead_time_s * estimator.dead_time_limits[1] + 0.02
    delays = np.array(sorted({min(high, max(low, current + 0.01 * k)) for k in range(-4, 5)}))
    windows = np.array(
        [
            (t, a if a is not None else np.nan, b if b is not None else np.nan)
            for t, a, b in estimator._windows
        ]
    )
    log = np.array(estimator._log)
    tau, step = estimator.model.motor_tau_s, estimator.model.STEP_S
    start = windows[0, 0] - estimator.window_s - 5 * tau - step
    count = int((windows[-1, 0] - start) / step) + 2
    grid = start + np.arange(count) * step
    indices = np.searchsorted(log[:, 0], grid[:, None] - delays, side="right") - 1
    targets = log[np.maximum(indices, 0), 1:]
    targets[indices < 0] = 0
    blend = 1 if tau <= 0 else min(1, step / tau)
    speed = np.zeros((len(delays), 2))
    turn = np.zeros_like(speed)
    angles = np.empty((count, len(delays), 2))
    for i in range(count):
        speed += (targets[i] - speed) * blend
        turn += speed * step
        angles[i] = turn
    results = []
    for k in range(len(delays)):
        results.append(
            [
                (
                    np.interp(t, grid, angles[:, k, 0])
                    - np.interp(t - estimator.window_s, grid, angles[:, k, 0]),
                    np.interp(t, grid, angles[:, k, 1])
                    - np.interp(t - estimator.window_s, grid, angles[:, k, 1]),
                )
                for t in windows[:, 0]
            ]
        )
    return delays, np.array(results)


def run(variant, sweep=False):
    import sys

    sys.path.insert(0, str(Path(variant).resolve()))
    from siyi_sdk import configure_logging

    configure_logging(level="WARNING", trace=False)
    from siyi_sdk.tracking.point_lock import PointLock
    from siyi_sdk.tracking.control import GimbalPredictor, TurnRateEstimator
    from siyi_sdk.tracking.calibrate import _video_delay
    from siyi_sdk.tracking.gimbal import GimbalPointLock
    from siyi_sdk.tracking.attitude import AttitudeHistory
    from siyi_sdk.tracking.metrics import LockMetrics
    from siyi_sdk.firmware_tracking import FirmwareFrameParser, firmware_crc32
    import struct

    frames, truths = scene_frames()
    packets = encoded_fixture(frames)
    source = hashlib.sha256()
    for path in sorted((Path(variant) / "siyi_sdk").rglob("*.py")):
        source.update(path.relative_to(Path(variant)).as_posix().encode())
        source.update(path.read_bytes())
    result = {
        "source_sha256": source.hexdigest(),
        "clock": {name: vars(time.get_clock_info(name)) for name in ("monotonic", "perf_counter")},
        "environment": {
            "platform": platform.platform(),
            "opencv_threads": cv2.getNumThreads(),
            "cpu_count": os.cpu_count(),
            "machine": platform.machine(),
            "thread_environment": {
                k: os.environ.get(k)
                for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
            },
            "av": av.__version__,
            "opencv": cv2.__version__,
            "numpy": np.__version__,
        },
        "fixture": {
            "sha256": hashlib.sha256(b"".join(packets)).hexdigest(),
            "frames": len(frames),
            "bytes": sum(map(len, packets)),
        },
        "micro": {},
    }
    model = GimbalPredictor(0.04, 0.07)
    for i in range(80):
        model.record(i * 0.02, (math.sin(i * 0.2) * 20, math.cos(i * 0.15) * 8))
    result["micro"]["predictor"] = timed(lambda: model.turn_ahead(1.6), 300)
    history = AttitudeHistory(seconds=10)
    for t in np.linspace(0, 4, 201):
        history.add(t, 8 * math.sin(t * 4), 3 * math.cos(t * 3))

    async def send(*args):
        pass

    lock = GimbalPointLock(send=send, attitude=history)
    samples = np.array(history.samples)
    lock._frames = deque(
        (
            t,
            -np.interp(t - 0.2, samples[:, 0], samples[:, 1]),
            -np.interp(t - 0.2, samples[:, 0], samples[:, 2]),
        )
        for t in np.linspace(1, 3, 50)
    )

    def fit():
        lock.loop.video_delay_s = 0.18
        lock._refine_video_delay()

    result["micro"]["online_delay"] = timed(fit)
    img_t = np.linspace(0.8, 3.5, 150)
    img_v = np.sin(img_t * 4)
    result["micro"]["calibration_delay"] = timed(
        lambda: _video_delay(samples[:, 0], samples[:, 1], img_t, img_v), 30
    )
    result["micro"]["attitude_lookup"] = timed(lambda: history.at(2.35), 1000)
    refit = TurnRateEstimator(0.06, 0.08)
    for i in range(160):
        refit.record(i * 0.02, (20 * math.sin(i * 0.11), 8 * math.cos(i * 0.14)))
    refit._windows = deque((i * 0.02, 1.0, 0.4) for i in range(60, 160))

    def motor():
        refit.model.dead_time_s = 0.06
        refit._refine_dead_time()

    result["micro"]["motor_refit"] = timed(motor, 30)
    refit.model.dead_time_s = 0.06
    delays, predicted = motor_batch_candidate(refit)
    exact = np.array([refit._turns(float(d)) for d in delays])
    result["motor_candidate"] = {
        "timing": timed(lambda: motor_batch_candidate(refit), 30),
        "max_difference": float(np.max(np.abs(exact - predicted))),
    }
    decoded = av.VideoFrame.from_ndarray(frames[0], format="bgr24").reformat(format="yuv420p")
    result["micro"]["prepare_bgr"] = timed(
        lambda: cv2.resize(
            cv2.cvtColor(decoded.to_ndarray(format="bgr24"), cv2.COLOR_BGR2GRAY),
            (640, 360),
            interpolation=cv2.INTER_AREA,
        )
    )
    result["micro"]["prepare_gray"] = timed(
        lambda: cv2.resize(
            decoded.to_ndarray(format="gray"), (640, 360), interpolation=cv2.INTER_AREA
        )
    )

    def decode_all():
        codec = av.CodecContext.create("hevc", "r")
        codec.thread_type = "SLICE"
        n = 0
        for data in packets:
            n += len(codec.decode(av.Packet(data)))
        result["decoder"] = {
            "name": codec.name,
            "thread_type": str(codec.thread_type),
            "thread_count": codec.thread_count,
        }
        return n

    result["micro"]["hevc_decode_25"] = timed(decode_all, 3)
    blob = bytearray()
    for i, data in enumerate(packets):
        payload = struct.pack("<IH", i, 1) + data
        head = b"\x55\x66\xaa\xbb" + struct.pack("<BIHB", 2, len(payload), i, 0x90)
        packet = head + struct.pack("<I", firmware_crc32(head)) + payload
        blob.extend(packet + struct.pack("<I", firmware_crc32(packet)))
    blob = bytes(blob)

    def parse():
        parser = FirmwareFrameParser()
        for i in range(0, len(blob), 4096):
            parser.feed(blob[i : i + 4096])

    result["micro"]["private_parser"] = timed(parse, 10)
    metrics = LockMetrics()
    tracemalloc.start()
    for i in range(180000):
        metrics.add(i / 50, (0.1 + (i % 173) / 200, 0), (0, 0))
    result["metrics_retained_bytes"] = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    result["micro"]["hour_summary"] = timed(metrics.summary, 5)
    # Warm up the real tracker before the five timing runs.
    tracking_trial(PointLock, frames, truths)
    result["tracking_runs"] = [tracking_trial(PointLock, frames, truths) for _ in range(5)]
    result["quality"] = {}
    for profile in ("pan", "occlusion", "low_texture", "foreground", "loss", "skips"):
        result["quality"][profile] = {
            "bgr": tracking_trial(PointLock, frames, truths, profile=profile)
        }
        try:
            result["quality"][profile]["gray"] = tracking_trial(
                PointLock, frames, truths, fmt="gray", profile=profile
            )
        except cv2.error:
            result["quality"][profile]["gray"] = "unsupported"

    async def async_work():
        output = {}
        for combined in (False, True):
            await scheduler_trial(GimbalPointLock, AttitudeHistory(), frames, combined, packets)
            output["combined" if combined else "scheduler"] = [
                await scheduler_trial(GimbalPointLock, AttitudeHistory(), frames, combined, packets)
                for _ in range(5)
            ]
        output["overload"] = [await overload_trial() for _ in range(5)]
        output["raw_backlog"] = [await raw_backlog_trial() for _ in range(5)]
        return output

    result.update(asyncio.run(async_work()))
    result["profile_runs"] = {
        profile: [tracking_trial(PointLock, frames, truths, profile=profile) for _ in range(5)]
        for profile in ("occlusion", "loss")
    }
    if sweep:
        result["sweep"] = []
        originals = (
            PointLock.WORK_WIDTH,
            PointLock.TARGET_FEATURES,
            getattr(PointLock, "PYRAMID_LEVEL", 3),
            cv2.getNumThreads(),
        )
        try:
            for width, features, level, threads in itertools.product(
                (480, 640), (200, 300), (2, 3), sorted({1, 2, 4, originals[3]})
            ):
                PointLock.WORK_WIDTH, PointLock.TARGET_FEATURES, PointLock.PYRAMID_LEVEL = (
                    width,
                    features,
                    level,
                )
                cv2.setNumThreads(threads)
                tracking_trial(PointLock, frames, truths)
                trials = [tracking_trial(PointLock, frames, truths) for _ in range(5)]
                trial = dict(trials[0])
                trial["timing_us"] = {
                    k: statistics.median(t["timing_us"][k] for t in trials)
                    for k in trial["timing_us"]
                }
                trial["runs"] = trials
                result["sweep"].append(
                    {
                        "width": width,
                        "features": features,
                        "level": level,
                        "threads": threads,
                        **trial,
                    }
                )
        finally:
            PointLock.WORK_WIDTH, PointLock.TARGET_FEATURES, PointLock.PYRAMID_LEVEL = originals[:3]
            cv2.setNumThreads(originals[3])
    return result


def run_closed_loop(variant):
    """Five scored runs per existing closed-loop profile, after one warmup."""
    import sys

    sys.path.insert(0, str(Path(variant).resolve()))
    from siyi_sdk import configure_logging

    configure_logging(level="WARNING", trace=False)
    from tests.tracking.test_lock_benchmark import run_profile
    from tests.tracking.sim import make_ground
    from siyi_sdk.tracking.gimbal import GimbalPointLock
    from siyi_sdk.tracking.firmware_link import FirmwareLink

    async def sink(*args):
        pass

    fmt = getattr(FirmwareLink("127.0.0.1", on_frame=sink), "image_format", "bgr24")
    original_lock, original_update = GimbalPointLock.lock, GimbalPointLock.update

    def prepare(frame):
        return (
            av.VideoFrame.from_ndarray(frame, format="bgr24")
            .reformat(format="yuv420p")
            .to_ndarray(format=fmt)
        )

    def lock(self, frame, *args, **kwargs):
        return original_lock(self, prepare(frame), *args, **kwargs)

    async def update(self, frame, *args, **kwargs):
        return await original_update(self, prepare(frame), *args, **kwargs)

    GimbalPointLock.lock, GimbalPointLock.update = lock, update

    async def measure():
        ground = make_ground()
        scores = {}
        for name in ("jump", "swing", "drift", "drift_stalls"):
            await run_profile(ground, name)
            scores[name] = [await run_profile(ground, name) for _ in range(5)]
        return scores

    try:
        return {"image_format": fmt, "profiles": asyncio.run(measure())}
    finally:
        GimbalPointLock.lock, GimbalPointLock.update = original_lock, original_update


def run_memory(variant):
    """Separate allocation tracing from timing; native codec allocations are excluded."""
    import sys

    sys.path.insert(0, str(Path(variant).resolve()))
    from siyi_sdk import configure_logging
    from siyi_sdk.tracking.gimbal import GimbalPointLock
    from siyi_sdk.tracking.attitude import AttitudeHistory
    from siyi_sdk.tracking.metrics import LockMetrics

    configure_logging(level="WARNING", trace=False)
    frames, _ = scene_frames()
    packets = encoded_fixture(frames)

    async def measure():
        await scheduler_trial(GimbalPointLock, AttitudeHistory(), frames, True, packets)
        peaks = []
        for _ in range(5):
            tracemalloc.start()
            await scheduler_trial(GimbalPointLock, AttitudeHistory(), frames, True, packets)
            peaks.append(tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()
        return peaks

    peaks = asyncio.run(measure())
    tracemalloc.start()
    metrics = LockMetrics()
    for i in range(180000):
        metrics.add(i / 50, (0.1 + (i % 173) / 200, 0), (0, 0))
    retained, construction_peak = tracemalloc.get_traced_memory()
    metrics.summary()
    summary_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return {
        "scope": "Python/NumPy traced allocations; input fixture allocated before tracing; native codec and OS buffers excluded",
        "combined_peak_bytes_runs": peaks,
        "combined_peak_bytes_median": statistics.median(peaks),
        "metrics_retained_bytes": retained,
        "metrics_construction_peak_bytes": construction_peak,
        "metrics_summary_peak_bytes": summary_peak,
    }
