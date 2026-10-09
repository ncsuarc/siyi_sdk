"""Run reproducible baseline/final SDK workloads without touching tracked results.

Examples:
    python scripts/benchmark_sdk.py --baseline .tools/baseline --output .tools/benchmark_results.json
    python scripts/benchmark_sdk.py --variant . --output /tmp/jetson_sdk.json
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import importlib.metadata
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import threading
import time
import tracemalloc
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch


def median(values):
    """Return the sample median as a float."""
    return float(statistics.median(values))


def percentile(values, p):
    """Return an interpolated percentile with one-element support."""
    values = sorted(values)
    point = (len(values) - 1) * p / 100
    lo = int(point)
    return values[lo] * (1 - (point - lo)) + values[min(lo + 1, len(values) - 1)] * (point - lo)


def measure_crc(crc16):
    data = bytes(range(256)) * 16
    runs = []
    for _ in range(5):
        started = time.perf_counter()
        for _ in range(1000):
            crc16(data)
        runs.append(time.perf_counter() - started)
    return {"seconds_per_1000x4096_median": median(runs), "runs": runs}


def measure_parser(Frame, FrameParser):
    rng = random.Random(2026)
    blob = bytearray()
    seq = 0
    while len(blob) < 1024 * 1024:
        blob.extend(Frame(1, seq, seq & 255, rng.randbytes(64)).to_bytes())
        seq = (seq + 1) & 65535
    blob = bytes(blob)
    bulk, chunks = [], []
    for _ in range(5):
        parser = FrameParser()
        started = time.perf_counter()
        result = parser.feed(blob)
        assert result.frames if hasattr(result, "frames") else result
        bulk.append(time.perf_counter() - started)
        parser = FrameParser()
        started = time.perf_counter()
        for index in range(0, len(blob), 1024):
            parser.feed(blob[index : index + 1024])
        chunks.append(time.perf_counter() - started)
    mib = len(blob) / 1048576
    return {
        "input_mib": mib,
        "bulk_mib_s_median": mib / median(bulk),
        "chunks_1k_mib_s_median": mib / median(chunks),
        "bulk_runs_s": bulk,
        "chunk_runs_s": chunks,
    }


async def measure_control_video(
    SIYIClient, AbstractTransport, Frame, SIYIStream, StreamConfig, StreamFrame, StreamBackend
):
    import numpy as np
    from siyi_sdk import configure_logging

    configure_logging(level="WARNING", trace=False)

    class Echo(AbstractTransport):
        def __init__(self):
            self.connected = False
            self.queue = asyncio.Queue()

        async def connect(self):
            self.connected = True

        async def close(self):
            self.connected = False
            self.queue.put_nowait(None)

        async def send(self, data):
            request = Frame.from_bytes(data)
            self.queue.put_nowait(Frame(2, request.seq, request.cmd_id, b"ok").to_bytes())

        async def stream(self) -> AsyncIterator[bytes]:
            while (item := await self.queue.get()) is not None:
                yield item

        @property
        def is_connected(self):
            return self.connected

        @property
        def supports_heartbeat(self):
            return False

    class RecordedVideo:
        def __init__(self, config, image):
            self._image = image
            self.state = "running"

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def frame_generator(self):
            for _ in range(30):
                now = time.perf_counter()
                yield StreamFrame(self._image, now, 1920, 1080, "recorded")
                await asyncio.sleep(1 / 30)

    runs = []
    for _ in range(5):
        gc.collect()
        image = np.zeros((1080, 1920, 3), dtype=np.uint8)
        ages = []
        stream = SIYIStream(StreamConfig("rtsp://fixture", backend=StreamBackend.OPENCV))
        stream._select_backend = lambda: RecordedVideo(stream._config, image)
        stream.on_frame(lambda frame: ages.append((time.perf_counter() - frame.timestamp) * 1000))
        client = SIYIClient(Echo(), max_retries=0)
        tracemalloc.start()
        started_cpu = time.process_time()
        await client.connect()
        await stream.start()
        latencies = []
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        for _ in range(100):
            await asyncio.sleep(max(0, next_tick - loop.time()))
            started = time.perf_counter()
            assert await client._send_command(1, b"") == b"ok"
            latencies.append((time.perf_counter() - started) * 1000)
            next_tick += 0.01
        await stream.stop()
        await client.close()
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        runs.append(
            {
                "cpu_s": time.process_time() - started_cpu,
                "peak_traced_mib": peak / 1048576,
                "frame_age_p50_ms": percentile(ages, 50) if ages else None,
                "frame_age_p95_ms": percentile(ages, 95) if ages else None,
                "command_p50_ms": percentile(latencies, 50),
                "command_p95_ms": percentile(latencies, 95),
                "command_p99_ms": percentile(latencies, 99),
            }
        )
    return {
        "median": {
            key: median([run[key] for run in runs if run[key] is not None]) for key in runs[0]
        },
        "runs": runs,
    }


async def measure_shutdown(OpenCVBackend, StreamConfig):
    class DelayedThread:
        def join(self, timeout):
            time.sleep(0.2)

        def is_alive(self):
            return False

    delays = []

    async def timer():
        for _ in range(20):
            began = time.perf_counter()
            await asyncio.sleep(0.01)
            delays.append(max(0.0, time.perf_counter() - began - 0.01) * 1000)

    backend = OpenCVBackend(StreamConfig("rtsp://fixture"))
    backend._thread = DelayedThread()
    await asyncio.gather(timer(), backend.disconnect())
    return {"timer_p99_ms": percentile(delays, 99), "timer_max_ms": max(delays)}


async def measure_backlog(OpenCVBackend, StreamConfig):
    import numpy as np

    class Capture:
        def __init__(self, stop):
            self.count = 0
            self.stop = stop

        def isOpened(self):
            return True

        def read(self):
            if self.count >= 30:
                self.stop.set()
                return False, None
            self.count += 1
            return True, np.full((1080, 1920, 3), self.count, dtype=np.uint8)

        def release(self):
            pass

    peaks = []
    for _ in range(5):
        backend = OpenCVBackend(StreamConfig("rtsp://fixture"))
        cap = Capture(backend._stop_event)
        if hasattr(backend, "_read_loop"):
            backend._loop = asyncio.get_running_loop()
            backend._queue = asyncio.Queue(maxsize=1)
            target = lambda: backend._read_loop(cap)
        else:
            from siyi_sdk.stream._delivery import LatestFrames

            backend._delivery = LatestFrames()
            backend._open = lambda: cap
            target = backend._worker
        gc.collect()
        tracemalloc.start()
        worker = threading.Thread(target=target)
        worker.start()
        worker.join()  # Deliberately keep the event loop stalled during 30 frames.
        peaks.append(tracemalloc.get_traced_memory()[1] / 1048576)
        tracemalloc.stop()
        await asyncio.sleep(0)  # Drain old scheduled callbacks after measurement.
    return {"traced_peak_mib_median": median(peaks), "runs": peaks}


async def measure_fps_history(SIYIStream, StreamConfig, StreamFrame, StreamBackend):
    import numpy as np
    import siyi_sdk.stream.stream as stream_module

    clock = [0.0]

    class Clock:
        @staticmethod
        def monotonic():
            return clock[0]

    class Frames:
        def __init__(self, config):
            self.state = "running"

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def frame_generator(self):
            img = np.zeros((1, 1, 3), dtype=np.uint8)
            for i in range(108000):
                clock[0] = i / 30
                yield StreamFrame(img, clock[0], 1, 1, "fixture")
                if i % 1000 == 0:
                    await asyncio.sleep(0)

    stream = SIYIStream(StreamConfig("rtsp://fixture", backend=StreamBackend.OPENCV))
    stream._select_backend = lambda: Frames(stream._config)
    old_time = stream_module.time
    stream_module.time = Clock
    try:
        await stream.start()
        await stream._task
        count = len(stream._frame_times)
    finally:
        stream_module.time = old_time
        await stream.stop()
    return {"timestamps_retained_after_hour_30fps": count}


def run_child(variant):
    sys.path.insert(0, str(Path(variant).resolve()))
    from siyi_sdk import configure_logging

    configure_logging(level="WARNING", trace=False)
    from siyi_sdk import SIYIClient
    from siyi_sdk.protocol import Frame, FrameParser, crc16
    from siyi_sdk.stream import SIYIStream, StreamBackend, StreamConfig, StreamFrame
    from siyi_sdk.stream.opencv_backend import OpenCVBackend
    from siyi_sdk.transport.base import AbstractTransport

    async def async_metrics():
        metrics = {
            "control_100hz_video_1080p30": await measure_control_video(
                SIYIClient,
                AbstractTransport,
                Frame,
                SIYIStream,
                StreamConfig,
                StreamFrame,
                StreamBackend,
            ),
            "shutdown": await measure_shutdown(OpenCVBackend, StreamConfig),
            "fps_history": await measure_fps_history(
                SIYIStream, StreamConfig, StreamFrame, StreamBackend
            ),
        }
        metrics["blocked_loop_backlog"] = await measure_backlog(OpenCVBackend, StreamConfig)
        return metrics

    metrics = {"crc": measure_crc(crc16), "parser": measure_parser(Frame, FrameParser)}
    metrics.update(asyncio.run(async_metrics()))
    return metrics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", default=".")
    parser.add_argument("--baseline")
    parser.add_argument("--output", required=True)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--performance", action="store_true")
    parser.add_argument("--closed-loop", action="store_true")
    parser.add_argument("--memory", action="store_true")
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--stage", action="append", default=[], metavar="LABEL=PATH")
    args = parser.parse_args()
    if args.child:
        if args.memory:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from performance_workloads import run_memory

            print(json.dumps(run_memory(args.variant)))
        elif args.closed_loop:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from performance_workloads import run_closed_loop

            print(json.dumps(run_closed_loop(args.variant)))
        elif args.performance:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from performance_workloads import run

            print(json.dumps(run(args.variant, args.sweep)))
        else:
            print(json.dumps(run_child(args.variant)))
        return
    output = Path(args.output).resolve()
    root = Path(__file__).resolve().parents[1]
    tracked = root / "tests/benchmarks/results.json"
    if output == tracked.resolve():
        parser.error("Refusing to overwrite the tracked benchmark baseline")
    output.parent.mkdir(parents=True, exist_ok=True)
    results = {
        "platform": platform.platform(),
        "python": sys.version,
        "interpreter": sys.executable,
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("numpy", "opencv-python", "aiortsp", "av", "structlog")
        },
        "workload": "100 Hz control, concurrent 1080p/30 video; 5-run medians; synthetic inputs",
        "logging": "WARNING, trace disabled",
    }
    if any("=" not in stage for stage in args.stage):
        parser.error("--stage must be LABEL=PATH")
    stages = [tuple(stage.split("=", 1)) for stage in args.stage]
    for label, variant in [("baseline", args.baseline), *stages, ("final", args.variant)]:
        if variant is None:
            continue
        command = [
            sys.executable,
            "-I",
            str(Path(__file__).resolve()),
            "--child",
            "--variant",
            variant,
            "--output",
            str(output),
        ]
        if args.performance:
            command.append("--performance")
        if args.closed_loop:
            command.append("--closed-loop")
        if args.memory:
            command.append("--memory")
        if args.sweep and label == "final":
            command.append("--sweep")
        env = dict(os.environ)
        completed = subprocess.run(command, capture_output=True, text=True, env=env)
        if completed.returncode:
            raise RuntimeError(f"{label} benchmark failed: {completed.stderr[-4000:]}")
        results[label] = json.loads(completed.stdout)
        print(f"Completed {label}", flush=True)
        output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    if args.performance:
        results["workload"] = (
            "Offline tracking/control/decode, five measured runs after warmup; see per-workload parameters"
        )
    if args.closed_loop:
        results["workload"] = (
            "Six-second simulated closed-loop profiles; five runs each after profile warmup; decoded image-format conversion"
        )
    if args.memory:
        results["workload"] = "Separate five-run allocation tracing; no timing claims"
    output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
