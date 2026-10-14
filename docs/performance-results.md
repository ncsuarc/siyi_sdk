# Local performance results

Measured 2026-10-09. Baseline: `4b9de165dd1679bd9d6285347d19395ec8b91e74`. Final: the uncommitted/staged SDK changes based on that revision, identified by source SHA-256 `f5bb733c48b98e627ad024e647687aad9b4c0cb00c8cc3eefaa193ed77bb5134`. No final commit was created by this work.

The main savings are in prediction, delay fitting, grayscale preparation, and retained metrics storage. These are **local Windows measurements**, not Pi estimates or physical-camera validation. The combined workload and simulated pointing results are mixed; there is no demonstrated across-the-board application or pointing speedup.

## Timing and memory savings

Timings below are the median of five per-run medians, after warmup. Percent reduction is `(baseline - final) / baseline`; separate effects must not be added together.

| Calculation | Baseline | Final | Reduction |
| --- | ---: | ---: | ---: |
| Predictor, 40 ms dead time / 70 ms motor lag | 54.800 us | 6.300 us | 88.5% |
| Online video-delay fit | 1.100 ms | 0.111 ms | 89.9% |
| Calibration video-delay fit | 2.437 ms | 0.167 ms | 93.2% |
| Attitude interpolation | 3.000 us | 0.700 us | 76.7% |
| Exact one-hour metrics summary | 5.936 ms | 4.126 ms | 30.5% |
| 720p image preparation: decoded BGR-to-gray versus direct gray, within final process | 0.629 ms | 0.211 ms | 66.4% |

Image preparation excludes compressed decoding, optical flow, and thread handoff. Grayscale remains full resolution until tracker downscaling; it changes image intensities and is not a numerically identical replacement.

| Memory workload | Baseline | Final | Interpretation |
| --- | ---: | ---: | --- |
| 180,000 samples: retained exact metrics | 5.495 MiB | 1.429 MiB | 74.0% reduction |
| Same metrics, temporary summary high-water | 7.550 MiB | 7.602 MiB | 0.7% increase |
| Combined workload: median traced allocation high-water | 1.406 MiB | 1.128 MiB | 19.7% reduction |

Memory tracing ran separately from timing. `tracemalloc` excludes native codec and OS socket buffers; combined-workload input images were allocated before tracing. Compact storage reduces retained metrics memory substantially, but boxing/sorting exact samples means the temporary summary peak is approximately unchanged. Exact lifetime statistics still grow with lock duration. Async summaries move work out of the callback but do not eliminate Python GIL contention.

## Scheduling, decoding, and overload

The requested controller rate is 50 Hz. The combined workload runs real software HEVC decoding and image preparation, real optical flow, attitude updates, online fitting, and a stub command sink. It excludes physical command/network delay and ground preview work. The frame producer is paced with `perf_counter` at 25 fps; earlier exploratory results using plain short sleeps were replaced because Windows early wakeups changed the input rate.

| Combined workload, five-run medians | Baseline | Final |
| --- | ---: | ---: |
| Actual control cycles/s | 62.649 | 50.182 |
| Emitted commands/s | 60.783 | 48.844 |
| Process CPU seconds during approximately 1.5 s | 1.578 | 0.984 |
| Control interval median, ms | 15.483 | 15.552 |
| Control interval p95, ms | 29.370 | 31.092 |
| Control interval p99, ms | 30.844 | 34.292 |
| Control interval max, ms | 30.995 | 42.850 |
| Event-loop lag p95, ms | 6.000 | 6.000 |
| Event-loop lag max, ms | 22.000 | 21.000 |

This host reports a 15.625 ms monotonic clock resolution. The old sleep-based loop can run above the configured rate under native-worker wakeups, or below it when each step sleeps. Deadline scheduling corrects the requested cadence and skips overdue cycles; it does not establish lower Windows p95/max gaps. In the separate 5 ms asynchronous-step workload, the median observed frequencies were 32.44 and 32.79 Hz, below 50 Hz in both cases.

The staged run isolates scheduler-only and numerical changes before the integrated image/queue work:

| Stage | Cycles/s | Interval p95, ms | Maximum interval, ms | CPU seconds |
| --- | ---: | ---: | ---: | ---: |
| baseline | 63.48 | 27.91 | 31.11 | 1.094 |
| scheduling | 50.19 | 31.06 | 40.92 | 0.859 |
| math | 50.10 | 31.06 | 32.18 | 1.234 |
| final | 50.08 | 31.06 | 44.93 | 1.109 |

For the speculative optimizations, the scheduling-only stage is the rate-matched reference: staged final p95 was essentially unchanged and its median maximum interval was 9.8% higher, within the plan's 10% limit. This narrow local result does not establish a hardware latency guarantee.

Maximum gaps and CPU usage varied across repeated runs; some final/staged comparisons regressed. The scheduling change is retained for deadline semantics, not a claimed throughput gain. The preceding preliminary staged run is retained in `staged-results.json`; its combined maxima differ from the later runs. Do not treat a favorable individual run as a guaranteed improvement. Pi tail-latency acceptance remains pending.

In the separate 8 MiB loopback workload, the event loop is deliberately stalled while the socket reader runs. The final raw buffer stays within 4 MiB; all 8 MiB arrive intact. Terminal state has separate capacity, so a full buffer cannot lose EOF/errors or prevent shutdown. The receive staging allocation and OS buffers are outside that 4 MiB budget.

| Slow-consumer workload, five-run medians | Baseline | Final |
| --- | ---: | ---: |
| Maximum compressed packets waiting | 4.000 | 1.000 |
| Delivered pictures out of 30 decoded | 11.000 | 11.000 |
| Wait after producer completion for final picture, ms | 0.000 | 16.000 |
| Delivered estimated age median, ms | 15.000 | 49.000 |
| Delivered estimated age p95, ms | 49.500 | 58.500 |

This 25 fps producer / 120 ms consumer experiment demonstrates independent draining and supersession, **not reduced frame age**: delivered age increased. It includes fake decoding to isolate handoff behavior; timer phase and whether the newest decode has completed affect which picture is available. The separate encoded workload checks real HEVC. Receipt-age checks invalidate pictures that exceed 400 ms before dispatch, after decoding, or while waiting for the consumer. These are not exposure-age measurements.

## Image and control quality

Grayscale is the firmware tracking default because all paired quality gates passed. The ground preview explicitly selects BGR and RTSP `StreamFrame` stays BGR. Compared with the color path, the worst added RMS error was 0.0164 pixels and the worst added maximum error was 0.0269 pixels across pan/rotation/zoom, occlusion, low texture, moving foreground, complete loss, and skipped-frame sequences. All success/loss decisions matched. Limits were 0.25 additional RMS pixels and one additional maximum pixel.

The following are medians of five six-second simulated runs after a warmup for each profile. Arrows show baseline BGR -> final grayscale with the updated controller. Effects of scheduling and image conversion are not isolated here.

| Profile | All-sample RMS, deg | Steady RMS, deg | Settling, s | Overshoot, deg | Reversals/s, yaw/pitch |
| --- | --- | --- | --- | --- | --- |
| jump | 1.284 -> 1.262 | 0.201 -> 0.182 | 0.500 -> 0.656 | 5.171 -> 4.529 | 0.33/0.00 -> 0.50/0.00 |
| swing | 3.134 -> 3.336 | — -> — | — -> — | 4.561 -> 4.958 | 1.17/0.00 -> 1.51/0.00 |
| drift | 0.885 -> 0.897 | 0.124 -> 0.115 | 0.328 -> 0.375 | 0.045 -> 0.032 | 0.00/0.00 -> 0.34/0.34 |
| drift_stalls | 0.965 -> 1.101 | 0.150 -> 0.238 | 0.343 -> 0.500 | 0.386 -> 0.384 | 0.67/0.34 -> 1.01/0.67 |

Jump overshoot and steady drift error improved in these runs, while jump settling, swing RMS, and stall-profile RMS worsened. One of five final `drift_stalls` runs retained an unsettled event at the six-second cutoff; the baseline had none. Existing closed-loop acceptance tests passed, but this is not evidence of uniformly better pointing. Do not tune gains or deploy based on desktop timing alone.

## Candidates and unchanged defaults

- Predictor and delay-fit changes preserve the discrete integration model, candidate grid, signs, interpolation, confidence checks, clipping and smoothing. Randomized predictor cases matched exactly; degenerate fits retain the original general solver.
- The batched motor-refit prototype matched simulated turns within roughly `1.2e-14` but was slower, so the runtime motor refit is unchanged.
- The initial unconditional parser copy prototype did not show a consistent benefit on ordinary encoded fixtures. A later size-specific comparison justified eliminating intermediate copies only above 64 KiB. Both CRC checks, corruption recovery and immutable payload ownership remain unchanged.

| Private parser workload: ten packets, fragmented into 4 KiB reads | Baseline median | Final median | Reduction |
| --- | ---: | ---: | ---: |
| 5,000 payload bytes per packet | 0.077 ms | 0.074 ms | 4.5% |
| 65,536 payload bytes per packet | 0.677 ms | 0.652 ms | 3.8% |
| 300,000 payload bytes per packet | 3.689 ms | 3.489 ms | 5.4% |

- The 32-configuration sweep covered working widths 480/640, feature counts 200/300, pyramid levels 2/3, and OpenCV threads 1/2/4/16, with warmup and five runs per configuration. The fastest median on this pan/rotation/zoom fixture was 1.402 ms at width 480, 200 features, pyramid level 2, 16 threads. This is insufficient to select Pi defaults; width 640, 300 features, and pyramid level 3 remain unchanged.
- RTSP conversion coalescing retains serial compressed decoding and publishes after at most eight decoded pictures or when caught up. Generic GStreamer dropping now precedes color conversion. No GStreamer speedup is claimed: `gi` is unavailable here.
- The measured software decoder was `hevc`, slice threading, 16 actual threads. No Pi hardware decoder was selected or implemented. Direct Y-plane conversion remains experimental.

## Validation and reproduction

Environment: Windows-11-10.0.26200-SP0; Python 3.12.15; NumPy 2.2.6; OpenCV 5.0.0.93; PyAV 19.0.0. OpenCV default thread count: 16. Encoded fixture SHA-256: `e49d3ecff5db8ccbec0e5b44bce981214ee6340e715f99b7b8dbbb97294a9a42` (25 pictures, 50,204 bytes). The encoder generates a deterministic synthetic HEVC fixture; it is not a camera recording.

- Integrated tracking/transport/stream/UI run: 285 passed, 8 skipped, 1 hardware test deselected, and one missing-`httpx` test-client dependency failure. Installing `httpx` in the local venv and rerunning that test passed. No runtime dependency was added to the project.
- After the final parser change: 20 numerical/quality/parser checks and 12 firmware/TCP checks passed. These overlap existing tests and must not be summed as independent coverage.
- Ruff checks, Ruff/Black format checks, strict mypy (47 SDK files), and Git whitespace checks passed. A pytest-asyncio fixture deprecation warning remains.
- Unavailable: native GStreamer (`gi`), seven `socat` serial checks, Pi hardware acceleration/thermal measurements, and physical camera/gimbal validation.
- A pre-existing lifecycle test could launch a real firmware-link task despite mocking its command client. It now injects a fake link and leaves no such pending task.

Raw artifacts are local, ignored files under `.tools/performance/`: `release-results.json` (final baseline comparison and sweep), `final-results.json` (staged results before the parser-only change), `closed-loop-results.json`, `memory-results.json`, `parser-size-results.json`, and validation logs. Closed-loop and memory measurements preceded the last parser-only change; those workloads bypass the private parser, and their measured code paths are unchanged. `comparison.json` is exploratory and has superseded producer pacing; do not use it for application claims.

From the repository root, using a fresh baseline directory:

```powershell
New-Item -ItemType Directory -Force .tools/performance | Out-Null
git archive --format=zip --output=.tools/performance/baseline.zip 4b9de165dd1679bd9d6285347d19395ec8b91e74
Expand-Archive .tools/performance/baseline.zip .tools/performance/baseline -Force
.\.venv\Scripts\python.exe scripts/benchmark_sdk.py --performance --baseline .tools/performance/baseline --variant . --sweep --output .tools/performance/rerun.json
.\.venv\Scripts\python.exe scripts/benchmark_sdk.py --closed-loop --baseline .tools/performance/baseline --variant . --output .tools/performance/rerun-closed-loop.json
.\.venv\Scripts\python.exe scripts/benchmark_sdk.py --memory --baseline .tools/performance/baseline --variant . --output .tools/performance/rerun-memory.json
```

`--stage LABEL=PATH` measures additional isolated trees. The preserved local scheduling stage replaces only the control-loop method; the math stage replaces the gimbal, control, attitude, calibration and metrics modules. Each source tree is fingerprinted in the JSON. Run timing workloads without concurrent tests, builds or allocation tracing. Repeated timings remain subject to desktop load and OS timer behavior.

Pi acceptance still requires the same before/after workload, actual decoder/backend identification, representative video, sustained thermal operation, frame-age/control-tail measurements and pointing checks. Hardware HEVC support must be verified in the installed Pi software stack, including output transfer and buffering. No desktop-to-Pi multiplier or hardware-performance claim is made.
