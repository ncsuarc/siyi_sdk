# Performance improvement plan

Originally reviewed 2026-10-09 at commit `6c28ace`. The findings and prototype measurements below are retained as planning history. Local implementation is now based on `4b9de165dd1679bd9d6285347d19395ec8b91e74`; see [performance results](performance-results.md) for measured changes, acceptance evidence, and remaining target validation.

Deployment scope confirmed by the user: Raspberry Pi 5 runs decoding, tracking, and camera control. The web UI runs on the ground and is outside this optimization scope. Desktop measurements guide candidate selection; acceptance requires measurements on the Pi 5.

## Findings and evidence

| Priority | Finding | Source | Effect |
| --- | --- | --- | --- |
| 1 | Control cycles sleep for a full period before doing work. | `siyi_sdk/tracking/gimbal.py:461` | Sending and computation extend the requested control period. |
| 2 | Firmware decoding waits for the frame consumer, and both compressed-video and TCP receive queues are unbounded. | `siyi_sdk/tracking/firmware_link.py:131`, `:248`, `:278`; `siyi_sdk/transport/threaded_tcp.py:110`, `:161` | Slow tracking or a stalled event loop can accumulate old video and memory. |
| 3 | Prediction rescans command history; delay fitting repeats general-purpose fits and simulations. | `siyi_sdk/tracking/control.py:190`, `:374`; `siyi_sdk/tracking/gimbal.py:421` | Repeated computation consumes control-loop time on the Pi. |
| 4 | Optical flow dominates tracker CPU time. | `siyi_sdk/tracking/point_lock.py:99` | Tracker parameter tuning offers more potential than small Python allocation changes, but can reduce accuracy. |
| Ground only | Preview processing and browser log rendering have optimization opportunities. | `web_ui/server.py:516`; `web_ui/static/protocol_log.js:2` | Recorded for context; excluded from Pi work. |

Local synthetic measurements using the repository venv, Python 3.12.15, OpenCV 5.0.0, NumPy 2.2.6 on Windows:

- The existing control-loop method, with a stub step and a requested 50 Hz rate, ran at approximately 32 Hz with no added step delay and 22 Hz with a requested 5 ms asynchronous step delay. Windows timer granularity contributes; fixing scheduling does not guarantee 50 Hz on every host.
- Local-model tracking on synthetic 1280x720 pans, 59 updates per run and three runs, measured 2.206 ms median and 2.671 ms p95 at the default 16 OpenCV threads. One thread was slower: 5.193 ms median. A separate default-thread profile attributed 68% of tracker time to the two optical-flow calls.
- A synthetic 3840x2160 preview resize to 1280x720 measured 1.078 ms median and 1.231 ms p95 over 30 iterations. This excludes copying, overlays, and JPEG encoding.

These are directional measurements, not camera latency or tracking-quality proof. No hardware was contacted. The benchmark script currently uses synthetic video callbacks and does not exercise actual optical flow, encoded-video queueing, or the dashboard preview; extend it before making end-to-end performance claims.

## Implementation order

### 1. Establish reproducible measurements

Extend `scripts/benchmark_sdk.py` to include the actual control-loop scheduler, optical flow, predictor/refits, and delayed firmware frame consumers. Save outputs under `.tools/`, preserving the tracked parser baseline. Exclude web UI JPEG/overlay work from the Pi workload.

Record configured/actual command frequency, interval p50/p95/p99 and maximum gap, event-loop lag, tracker/predictor/refit time, compressed queue bytes/oldest arrival age, decode batch size/time, decoded frames superseded, and memory high-water mark. Distinguish estimated capture age from receive-queue residence time.

Run the same Pi workload before and after each change. Include normal and slow tracking, repeated parameter refits, a stalled event loop, and repeated shutdown/reconnect. Use loopback transports and fake decoders for overload/recovery checks, then a real encoded fixture to check decoding dependencies. Keep external data/control traffic representative of deployment without assuming a new ground-link architecture.

### 2. Fix control-loop scheduling

Change `GimbalPointLock._control_loop` to schedule against monotonic deadlines. Advance the deadline by the configured period and sleep only for the remaining time. If work or a stall passes subsequent deadlines, skip missed cycles instead of sending catch-up command bursts. Recheck activity after waking.

Continue passing actual elapsed time to `_control_step`; preserve the existing stalled-integral protection, stale-attitude behavior, serialized sends, and release stop command. Validate that `control_hz` is finite and positive before using it.

Acceptance:

- A step shorter than the requested period consumes part of that period rather than adding an entire period of sleep.
- Overruns skip deadlines; they do not produce queued catch-up commands.
- Cancellation/release prevents later steering and retains the final rate-mode stop.
- Existing closed-loop simulations still meet pointing error, overshoot, settling, and jitter checks. Report operating-system scheduling limits separately from algorithmic drift.

Use existing `tests/tracking/test_point_lock.py`, `test_closed_loop.py`, and `test_lock_benchmark.py`, with focused scheduling/overrun coverage where needed.

### 3. Separate firmware decoding from tracking and bound overload

First, give each connection one owned decoder and one serialized decoded-frame consumer. Decode compressed access units in order, then publish only the newest decoded image into a replaceable slot. A slow tracking callback must not prevent the decoder from draining compressed data. Await the consumer during teardown and invalidate pending frames when the connection changes.

Keep the decoded image and its associated estimated capture timestamp together. Verify their association with a decoder that produces delayed or no output: the current code labels any returned image with the last packet's timestamp. Avoid making that assumption part of the new handoff.

Then add separate byte limits for raw TCP chunks and complete compressed access units, plus an oldest-arrival-age limit for the compressed queue. Include active decode batches in memory accounting. Exact limits are proposed tuning values to select from measured bursts and existing stale-video behavior, not established camera requirements.

For `ThreadedTCPTransport`, reserve capacity before posting data to the event loop. Use a thread-safe bounded buffer with coalesced notification and reader backpressure; merely adding `asyncio.Queue(maxsize=...)` would leave scheduled callbacks unbounded. Preserve every byte and its arrival timestamp. Shutdown must wake a reader waiting for capacity, and EOF/errors must still reach the consumer when the data buffer is full.

For firmware compressed-video overflow or excess queue age, invalidate delivery and fail the current session through an explicit overload path. Tear down the connection and decoder, then reuse reconnect/AI-mode-off recovery. Preserve compressed-frame dependencies rather than dropping arbitrary access units. Propagate failure to the tracking owner so an active lock is released promptly; retain the existing unconfirmed-exit behavior when mode-off cannot be confirmed. Reconnection must not automatically resume steering.

Native decode lifetime is part of this change: cancelling an await on `asyncio.to_thread` does not stop its worker. Capture the connection's decoder in the job and wait for active work before replacing it or starting another job. If native work cannot finish within the shutdown policy, mark the connection failed and prevent decoder reuse/concurrent access.

Acceptance:

- Slow consumers see fresh decoded pictures while decoding remains serial and compressed inputs remain ordered.
- Application buffering has a demonstrated finite bound, including pending thread notifications, the reader's staging buffer, decode batches, and decoded image slots. OS socket buffers are reported separately.
- Overflow causes visible release/recovery; stale or previous-connection frames never reach steering.
- Normal bursts, arbitrary TCP chunk boundaries, keyframe startup, packet corruption, EOF, cancellation, and reconnect remain supported.
- Increased frame skipping does not regress tracker accuracy or loss behavior beyond the existing guards.

Use `tests/transport/test_threaded_tcp.py`, `tests/tracking/test_firmware_link.py`, `test_firmware.py`, and `test_firmware_over_tcp.py`, adding focused slow-consumer, overload, full-buffer shutdown, and in-flight decode recovery cases.

### 4. Simplify Python prediction and fitting

Replace repeated predictor history scans with one advancing index, preserving the discrete integration grid and numerical behavior. Batch the online video-delay fit, preserving all rejection/confidence checks and smoothing. Profile motor-delay fitting on the Pi before selecting its implementation. Detailed proposals and desktop prototype measurements appear in the Python math review below.

If calibration runs on the Pi, batch the SDK calibration fits and keep image processing/fitting off the control event loop through serialized jobs. Drain active recording work before snapshotting or replacing samples. Preserve timestamps, frame order, degenerate-input handling, and motion-stop cleanup. Calibration placement has not been separately specified.

Acceptance:

- Predictor results and accepted delay estimates match existing behavior within justified numerical tolerances.
- Pi control interval tails and maximum gaps improve under repeated fitting; gains are measured under sustained decode/tracking load.
- Existing prediction, calibration, adaptation, and closed-loop quality checks pass.

Use the tracking control/closed-loop suite and focused equivalence checks. Web UI changes and browser validation are excluded.

### 5. Tune optical flow only after the preceding changes

Compare the current 640-pixel working width, 300 features, and pyramid depth of 3 against a small parameter sweep on the intended host. Evaluate pan, rotation, zoom, low texture, occlusion, moving foreground, skipped frames, and the existing control benchmark profiles. Measure tracking error, inliers/losses, control response, and CPU time together.

Keep the forward/backward consistency check and robust motion rejection unless evidence supports a replacement. Keep current defaults until a candidate provides useful speed improvement without tracking regressions. Do not globally force one OpenCV thread: the local measurements showed a substantial slowdown.

## Completion boundary

Implement and validate each package separately so regressions are attributable. Run relevant existing checks after each change, then the tracking and transport suites for the integrated result. Record remaining hardware validation explicitly: offline success cannot establish physical pointing quality, network latency, or confirmed firmware exit on a real camera.

## Expected gains and additional candidates

These estimates describe separate effects and must not be added into an overall speedup. At the original review, no fixes had been implemented or benchmarked end to end; current implementation measurements are in `performance-results.md`.

| Planned change | Defensible estimate | Limits |
| --- | --- | --- |
| Deadline scheduling | With ideal timers, a 20 ms sleep plus a 5 ms step currently produces 40 Hz. Sharing the same 20 ms cycle between sleep and work targets 50 Hz: 25% more updates. | Illustrative arithmetic, not a measured improvement. Windows wake-up granularity and sends longer than the cycle can prevent 50 Hz. |
| Separate decoding, fresh-frame handoff, bounded queues | Little improvement when queues are empty; potentially substantial reduction in stale-frame residence under overload. Ten queued 25 fps pictures span approximately 360 ms from oldest to newest. | Does not remove source/network delay. Backpressure or overflow recovery can interrupt delivery; skipping frames needs tracking-quality checks. |
| Preview workers, ground only | The measured resize moves about 1.1 ms per 4K preview off the ground event loop. | Excluded from the Pi plan and Pi improvement estimates. |
| Optical-flow tuning | At the measured 68% tracker-time share, reducing optical-flow time by 25% would reduce total tracker time by about 17%, assuming other work is unchanged. | A conditional example; no such tuning improvement has been demonstrated, and tracking accuracy may worsen. |

Additional findings, in suggested order:

1. **Batch the online video-delay fit.** `GimbalPointLock._refine_video_delay` (`siyi_sdk/tracking/gimbal.py:421`) evaluates 25 candidate delays with a separate degree-one polynomial fit for each of two axes, every five accepted tracking frames. In a local synthetic measurement with 50 frame samples and approximately 150 attitude samples, the existing routine took 1.111 ms median and 1.728 ms p95 over 100 runs. An in-memory prototype computed candidate costs with centered least-squares arithmetic batched over delays: 0.094 ms median and 0.244 ms p95. The maximum cost difference was about `1.7e-15`, and both selected 0.20 s. This is approximately 90% less time for this calculation, not a whole-application improvement or a production-ready fix. Preserve insufficient-history rejection, signs, confidence checks, delay clipping, smoothing, and degenerate-time behavior. Check the motor-dead-time refit separately for periodic event-loop stalls.
2. **Bound long-lock metrics and avoid synchronous sorting on release.** `LockMetrics._errors` and `_events` grow throughout a lock (`siyi_sdk/tracking/metrics.py:49`). A synthetic one-hour steady lock at 50 Hz retained 180,000 errors, about 5.5 MiB for that list and its floats, and `summary()` took 22.66 ms median over three runs. Maintain exact running sums/counts for RMS; snapshot/offload final summaries. If bounding the percentile/event sample storage, explicitly choose and document whether the result is an approximate whole-lock statistic or an exact recent-window statistic. Do not silently change the meaning of reported p95.
3. **Protocol-log UI work: deferred, ground only.** The browser row rebuilding and hidden-panel polling opportunities are outside the current Pi scope.
4. **Avoid color conversion for decoded RTSP pictures already superseded during catch-up.** `siyi_sdk/stream/aiortsp_backend.py:193` converts each decoded picture to BGR before publishing into a replaceable latest-frame slot. Preserve ordered compressed decoding but coalesce output conversion during a bounded catch-up batch. Bound the batch so publication is not postponed indefinitely. The firmware link already converts only the newest picture in a batch. Benefit depends on decode backlog and does not justify skipping every intermediate frame during normal streaming.
5. **Remove smaller repeated work only if profiling warrants it.** `AttitudeHistory.at` (`siyi_sdk/tracking/attitude.py:66`) allocates a timestamp list for each lookup; `PointLock.update` (`siyi_sdk/tracking/point_lock.py:129`) inverts an unchanged diagonal scale matrix each frame. Cache or eliminate those calculations without changing interpolation or coordinate transforms. Expected gains are small compared with optical flow and queue freshness.

The strongest next measurable CPU candidate is the batched video-delay calculation. The highest priority behavior fixes remain control scheduling and freshness under overload. A healthy 25 fps camera stream will still provide at most 25 new pictures per second after these changes; physical pointing improvements require camera measurements.

## Python math and loop review

Additional five-run microbenchmarks are reproducible with:

```powershell
.\.venv\Scripts\python.exe .tools\python-math-review\benchmark.py
```

The ignored scratch script and its `results.json` contain exploratory prototypes, not runtime patches. They contact no hardware. Timings are local Windows medians and do not describe whole-application speedups.

| Calculation | Existing median | Prototype median | Interpretation |
| --- | --- | --- | --- |
| `GimbalPredictor.turn_ahead`, 40 ms dead time / 70 ms motor lag, 28 retained commands | 56.83 us | 10.47 us | Approximately 5.4x faster by walking command history once. |
| Same predictor, 120 ms dead time / 150 ms motor lag, 56 retained commands | 191.10 us | 22.84 us | Approximately 8.4x faster on a longer integration/history window. |
| Calibration `_video_delay`, 160 candidates and 150 image samples | 2.75 ms | 0.69 ms | Approximately 4x faster by batching simple least-squares fits. |
| Motor dead-time refit, 100 windows / 127 commands | 2.04 ms | Not prototyped | Periodic synchronous Python work deserving further profiling. |
| Scalar PI controller | 3.18 us | Not prototyped | Already inexpensive. |
| Normal forward-time Kalman update | 2.46 us | Not prototyped | Already inexpensive; two-axis scalar arithmetic does not justify wholesale array conversion. |
| Pixel-to-angle math | 0.27 us | Not prototyped | Negligible at the current frame rate. |
| Attitude interpolation, 150 retained samples | 2.40 us | Not prototyped | Removing temporary timestamp allocation is a lower priority. |

### Predictor fix proposal

`GimbalPredictor.turned` currently calls `_commanded` for every 5 ms integration step, scanning command history from its beginning each time. Replace repeated scans with one monotonically advancing command index. Hoist the constant blend out of the loop and use explicit yaw/pitch accumulators to avoid repeated two-element loops and temporary lists. The search work changes from approximately integration-steps times retained-commands to integration-steps plus retained-commands.

Keep the existing Euler integration grid, initial warm-up, command boundary comparisons, accumulated-angle timing, zero-history behavior, and motor-lag semantics. The scratch prototype exactly matched the original on 100 randomized synthetic cases spanning zero/nonzero lag and irregular interval endpoints. This does not replace the existing control and closed-loop checks before production use.

The shorter-window saving is approximately 46 us per call: about 2.3 ms of computation per second for one call per 50 Hz cycle on this host. The function-level speedup is substantial, but the application-level CPU saving is modest. Slower target hardware and repeated predictor use may make it more valuable.

### Fit and math fix proposals

Prioritize batched online video-delay fitting, then the predictor scan, then calibration fitting and motor dead-time search. For calibration, the centered slope/intercept formula eliminates 160 separate general least-squares calls; the prototype matched lag and scale within `1e-12` on its synthetic input. Production handling must preserve singular/constant-input behavior, interpolation boundaries, ties, numerical stability, and existing calibration result validation.

The motor refit already advances through command history efficiently inside each candidate; do not assume the predictor's repeated-search fix applies unchanged. Profile its repeated simulation and interpolation, then compare an equivalent batched or cached implementation. A continuous-time analytic motor solution would change the current discrete model and needs separate control validation.

Avoid replacing the small scalar controller/filter with NumPy solely to remove Python loops. Likewise, caching trigonometry or replacing the fixed scale-matrix inverse should follow measured benefit. Existing public CRC uses `binascii.crc_hqx`, and private CRC uses native translation plus `zlib.crc32`; their heavy per-byte arithmetic is already outside Python loops.

## Raspberry Pi deployment requirement

The user confirmed Raspberry Pi 5 deployment for decoding, tracking, and camera control; the web UI runs on the ground and should be excluded. Cooling and OS/Python build are not yet confirmed. Desktop absolute times and function speedup ratios are exploratory evidence, not deployment acceptance results. Do not use a guessed desktop-to-Pi multiplier.

Retain the predictor history-scan and fit simplifications as meaningful candidates: they remove repeated work independently of processor speed. Re-evaluate their absolute cost and relative gains on the Pi. Treat the inexpensive desktop scalar math as lower priority, not as proven negligible on the target. Small repeated costs may matter when decoding, optical flow, and Python execution share the Pi.

Target validation sequence:

1. Copy the offline scratch benchmark into the same relative path on the Pi and run it with the deployment interpreter/environment. The script records architecture, CPU count, Pi model when available, Python/NumPy/OpenCV versions, OpenCV thread count, and relevant thread environment settings. Use the actual camera control configuration rather than assuming the synthetic motor parameters are representative.
2. Record baseline live decode, tracking, prediction/refit, and control timings before applying fixes. Compare the same workloads after each change on that Pi. Include application rate/angle tracking and firmware tracking where supported; exclude ground preview encoding, overlays, and browser work.
3. Record control interval p95/p99, maximum gap, event-loop lag, frame age, CPU usage, queue high-water marks, temperature, clock frequency, and throttling state. Include a sustained run that reaches stable operating temperature. A short cool-board benchmark is insufficient.
4. At a requested 50 Hz, the nominal cycle is 20 ms. Assess how much of that budget computation and sending consume, and whether occasional refits miss deadlines. The 25 fps video path has a nominal 40 ms picture interval; decoding and tracking must keep pace or replace old decoded pictures safely.
5. Compare a small set of OpenCV/native-library thread settings under the combined workload. The desktop's 16-thread result does not establish an appropriate Pi setting. Choose settings from total latency, deadline misses, thermal behavior, and tracking quality; isolated tracker throughput is insufficient.

Prioritize Pi measurements of the online video-delay fit, predictor and motor-delay refit alongside video decoding and optical flow. Compile a numerical kernel only if target profiling still shows a material Python cost after algorithmic simplification; no new numerical dependency or language rewrite is justified by desktop microbenchmarks alone.

## Additional review: image path and Pi decode (2026-10-09)

Reviewed against `4b9de16`. Only dependency metadata changed since the plan's `6c28ace` baseline; the runtime paths reviewed here are unchanged. These additions are proposals, not implemented optimizations. Keep the scheduling and bounded-freshness work first.

### 1. Feed grayscale images to the tracker without a BGR round trip

`FirmwareLink._decode` (`siyi_sdk/tracking/firmware_link.py:290`) converts the newest decoded picture to BGR. `PointLock._gray` (`siyi_sdk/tracking/point_lock.py:79`) immediately converts it back to grayscale and downsizes it to a 640-pixel working width. The RTSP PyAV backend also materializes BGR. A tracking-only image path can avoid the full-resolution color allocation and conversion. This saves work even without a backlog, unlike the existing proposal to skip conversion of superseded pictures.

First compare decoder-to-gray conversion with the existing path. A more aggressive option is resizing the decoder's Y plane directly. Preserve original image dimensions for target coordinates, ROI initialization, box sizes and pixel-to-angle calculations; passing a 640-wide array through the current API without that metadata would change its coordinate contract. Retain BGR support for existing consumers and only generate color when a consumer actually needs it.

An offline microbenchmark is saved in `.tools/additional-performance-review/benchmark.py`, with output in `results.json`. Run it with the repository venv. It uses a synthetic, already-decoded YUV420P image, 10 warmups and 100 timed calls per variant; it excludes compressed decoding, optical flow, handoff and camera latency. Local Windows / Python 3.12.15 / OpenCV 5.0.0 / PyAV 19.0.0 / NumPy 2.2.6, with 16 OpenCV threads:

| Image preparation path | 1280x720 median | 3840x2160 median |
| --- | --- | --- |
| Decoded image -> BGR -> gray -> resize | 0.499 ms | 2.413 ms |
| Decoded image -> gray -> resize | 0.109 ms | 1.461 ms |
| Decoded Y-plane view -> resize | 0.015 ms | 0.669 ms |

At 720p, direct gray saved approximately 0.39 ms per delivered picture in this isolated measurement. This is not a measured Pi or whole-tracker speedup. The repeated hot synthetic image is not representative video. Simply resizing BGR before converting it to gray did not improve median time at either tested size, so do not adopt that rearrangement based on intuition.

The images are not numerically interchangeable: at 720p, direct gray and direct Y differed from the current prepared image by mean absolute values of 1.33 and 2.83 intensity levels respectively. Color range, color matrix, chroma conversion and rounding matter. Handle pixel format, bit depth, row stride and decoder-plane lifetime explicitly; a view must not outlive its owning frame. Validate feature counts, accepted motion, drift and loss behavior on representative recordings before selecting this path. The 4K case is relevant only if the application tracker consumes a 4K stream; the firmware path is currently 720p.

### 2. Evaluate the Pi's HEVC hardware decoder explicitly

The private stream uses HEVC, and the current firmware link constructs a generic `av.CodecContext.create("hevc", "r")` with slice threading (`firmware_link.py:200`). It has no explicit hardware-decoder setup. Raspberry Pi's [official Pi 5 specifications](https://www.raspberrypi.com/products/raspberry-pi-5/) list a 4Kp60 HEVC decoder. Hardware capability does not establish that the installed PyAV/FFmpeg build can use it.

Add a target-only feasibility check: identify the decoder actually selected by the deployment build, then compare the current software path with an available Pi hardware path using the same encoded recording. Account for startup, buffering, output-surface transfer, grayscale preparation, CPU use and frame age. A faster decoder with extra buffering can still make tracking worse. Preserve ordered access units, per-frame timestamps, keyframe recovery and session teardown. Keep software decoding as a fallback; do not assume an encoder or hardware H.264 support from the HEVC capability. No hardware speedup or compatible decoder plugin was verified in this review.

Also make decoder thread count an explicit dimension of the already-planned combined-workload thread sweep. The firmware link sets SLICE but leaves the count implicit; the RTSP backend forces one decoder thread. Record actual codec settings with OpenCV settings instead of attributing all contention to optical flow.

### 3. Do backward optical flow only for successful forward tracks

`PointLock.update` (`point_lock.py:99-107`) sends all forward results through the backward LK call, then rejects points with a failed forward status. Filter those failures before the backward pass, and scatter results back to the original feature indices for the existing consistency check, score denominator and debug records. If fewer than eight forward points survive, the current homography threshold cannot be met: bypass the remaining flow/fit work and follow the existing lost/re-detect path.

This retains forward/backward validation for every eligible point and avoids work on points that cannot be accepted. Benefit should be greatest during partial occlusion and large motion; healthy frames with almost no forward failures may see no gain or a small indexing overhead. Measure both cases. Preserve original point ordering and verify accepted-feature masks and loss behavior; filtering is a candidate, not an established numerical-equivalence result. Reusing image pyramids is a separate exploratory option only if the deployed OpenCV Python binding supports the required input form efficiently.

### 4. Reduce private-video parser copies before considering a CRC rewrite

`FirmwareFrameParser.feed` copies the complete CRC input using a bytearray slice followed by `bytes(...)` (`firmware_tracking.py:184-186`). It similarly copies the payload, then `_read` slices off the six-byte video prefix (`:190`, `:336`). `firmware_crc32` also creates the reflected-byte buffer. Native CRC arithmetic is already efficient, but moving the large encoded frame repeatedly still consumes allocation and memory bandwidth on the event-loop path.

Profile allocation volume and parser time with realistic keyframe sizes, fragmented reads and coalesced bursts. Candidates include avoiding the intermediate bytearray slice, creating the owned video payload once, and releasing temporary memoryviews before resizing the parser buffer. Preserve public payload ownership, both CRC validations, corruption recovery and receipt timestamps. Do not keep views into the mutable parser buffer across feeds or let a small queued view retain an unexpectedly large burst allocation. Do not prioritize this over image-path work without target evidence; no parser speedup was measured here.

### 5. Place GStreamer overload dropping before color conversion

For deployments using the generic GStreamer RTSP backend, `_AUTO_PIPELINE` (`siyi_sdk/stream/gstreamer_backend.py:58-64`) places its leaky decoded-frame queue after `videoconvert`. If color conversion is the bottleneck, that queue cannot discard stale decoded pictures before they incur conversion cost.

Evaluate a bounded leaky queue immediately after decoding and before conversion, retaining bounded delivery at appsink. This drops only decoded pictures and keeps compressed-frame dependencies intact. Compare delivered-frame age and conversion count during deliberate downstream overload; this provides no guaranteed benefit in an unloaded pipeline. Include active conversion and appsink buffers in memory accounting. It is conditional on using this backend and is not a reason to replace the private firmware transport.

### Suggested placement

Measure direct grayscale and actual Pi HEVC decoding during step 1, and carry the selected image path into the serial-decoder/fresh-frame work in step 3. Evaluate filtering failed forward tracks before reducing feature count or pyramid depth in step 5. Keep parser-copy work and the GStreamer queue change conditional on the measured bottleneck and selected deployment backend. None of these candidates justifies changing tracker defaults or weakening stale-frame, confidence or release guards without validation.


## Implementation status (2026-10-09)

| Package | Status |
| --- | --- |
| Reproducible baseline/final measurement | Implemented in `scripts/benchmark_sdk.py --performance`, with isolated variant processes, staged snapshots, raw distributions, source/fixture hashes, and separate memory measurement. `--closed-loop` records five runs per simulated motion profile. |
| Deadline scheduling | Implemented with missed-cycle skipping, elapsed-time accounting, and positive finite rate validation. Windows timer limitations are reported separately. |
| Threaded TCP buffering | Implemented byte/count limits, reader backpressure, coalesced notifications, terminal-state delivery, and cancelled-connect socket cleanup. |
| Firmware decode/delivery | Implemented serial session-owned decoding, replaceable decoded delivery, packet/output timestamp association, receipt-age checks, bounded compressed batches, overload release callback, reconnect, and retained native-work ownership. |
| Predictor, online delay, calibration fits | Implemented and checked against reference behavior. Degenerate calibration/time fits retain the general solver. |
| Motor refit | Batched prototype measured slower; existing runtime implementation retained. |
| Image conversion | Grayscale firmware default enabled after paired image-quality and closed-loop checks. BGR remains selectable and is explicit for the ground preview; RTSP output stays BGR. Direct Y-plane conversion remains experimental. |
| Optical flow | Failed forward tracks are removed before backward LK; fixed scale-matrix inverse removed. Parameter/thread sweep recorded; existing width/features/pyramid defaults retained pending Pi validation. |
| Attitude interpolation | Temporary timestamp allocation removed while preserving interpolation and clamping. |
| Calibration and summaries | Async serialized recording/fitting and asynchronous final summaries added; synchronous interfaces remain available. |
| Exact metrics | Numeric sample storage compacted and RMS accumulated online. Exact whole-lock percentiles/event medians retained; lifetime storage still grows by the user's choice. |
| Private parser copies | Initial unconditional prototype was not retained. A size-specific comparison supported reducing intermediate copies only above 64 KiB; CRC checks, corruption recovery and owned payloads are preserved. |
| RTSP and GStreamer conversion | RTSP coalesces conversion within eight-picture catch-up batches. Generic GStreamer queue moved before color conversion; native GStreamer validation remains unavailable on this host. |
| Pi acceleration and deployment acceptance | Pending actual Pi software-stack inspection, thermal/load measurements, representative recordings, and camera validation. No hardware backend was added. |

The existing expected-gain examples and scratch prototypes above are not final implementation results. Do not sum their percentages or use them as Pi performance claims.
