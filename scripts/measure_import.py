"""Five fresh-process measurements of control-only package import overhead."""

from __future__ import annotations

import argparse
import ctypes
import json
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path


def current_rss() -> int:
    """Return process resident bytes using only the standard library."""
    if sys.platform == "win32":
        size = ctypes.c_size_t
        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
                (name, size) for name in (
                    "peak_working_set", "working_set", "peak_paged_pool", "paged_pool",
                    "peak_nonpaged_pool", "nonpaged_pool", "pagefile", "peak_pagefile", "private")]
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = (
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)
        if not ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return counters.working_set
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def main() -> None:
    """Write JSON timings/RSS for isolated source variants."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", default=".")
    parser.add_argument("--baseline")
    parser.add_argument("--output", required=True)
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args()
    if args.child:
        sys.path.insert(0, str(Path(args.variant).resolve()))
        tracemalloc.start()
        started = time.perf_counter()
        from siyi_sdk import SIYIClient, connect_udp
        elapsed = time.perf_counter() - started
        assert SIYIClient and connect_udp
        print(json.dumps({"import_ms": elapsed * 1000, "rss_mib": current_rss() / 1048576,
                          "traced_peak_mib": tracemalloc.get_traced_memory()[1] / 1048576,
                          "numpy_loaded": "numpy" in sys.modules}))
        return
    result = {"platform": platform.platform(), "python": sys.version, "runs": 5}
    for name, variant in (("baseline", args.baseline), ("final", args.variant)):
        if variant is None:
            continue
        runs = []
        for _ in range(5):
            command = [sys.executable, "-I", str(Path(__file__).resolve()), "--child", "--variant", variant, "--output", args.output]
            child = subprocess.run(command, capture_output=True, text=True, check=True)
            runs.append(json.loads(child.stdout))
        result[name] = {"median": {key: statistics.median(run[key] for run in runs) for key in ("import_ms", "rss_mib", "traced_peak_mib")},
                        "numpy_loaded": runs[0]["numpy_loaded"], "runs": runs}
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
