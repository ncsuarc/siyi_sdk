# A8 Mini SDK implementation and measured impact

## Scope and assumptions

This plan implements the applicable findings in [SDK_REVIEW.md](SDK_REVIEW.md) for the
SIYI A8 Mini. Its default H.264 URL is `rtsp://<camera-ip>:8554/main.264`.
Other camera APIs remain available, with explicit generation selection where needed.
Thermal delivery and H.265 implementation are outside this A8 project. Breaking
changes are accepted; this is not a migration or repository-transfer project.

There is no physical A8 Mini available. Local protocol/RTSP servers and a recorded
synthetic H.264 fixture validate software behavior only. Camera-specific ACK and
stream behavior remains **unverified** until the camera gate below is run. Linux
desktop and Jetson measurements are still required when those platforms are available;
current measurements use Windows and cannot predict Jetson improvements.

Estimated engineering effort: **17–28 days**, including regression and synthetic
integration tests. Physical hardware validation is additional. The stage estimates
overlap the total estimate and are planning estimates, not elapsed time spent here.

## Implementation sequence and acceptance

1. **A8 baseline (1 day).** Make package video exports lazy, explicitly declare
   NumPy for video extras/tests, default URLs to old-generation A8 `/main.264`,
   reject its unavailable `sub` selection, repair stale fixtures, and establish
   isolated wheel imports and deterministic benchmark inputs. Other camera
   generations retain explicit URL selection.
2. **Client lifecycle and subscriptions (3–5 days).** Supervise one reader and
   heartbeat per connection. EOF, reader errors, and heartbeat errors clear
   readiness, fail outstanding requests with SDK `ConnectionError`, stop tasks,
   close the transport, reset the parser, and retry after 0.5, 1, 2, 4, and 8
   seconds. Replay snapshots of separate FC and gimbal subscriptions before
   marking ready. Commands during reconnect raise `NotConnectedError`; replay
   uses an internal send path. Failed replay is torn down. Close is idempotent
   and keeps cleanup ahead of background error reporting. Pending futures are
   removed by identity in `finally`; subscriber callbacks use snapshots and
   isolated exceptions.
3. **Protocol and matching (3–5 days).** Parse buffered complete frames and
   return `ParseResult(frames, errors)`: malformed candidates do not hide valid
   neighbors, while partial tails stay buffered. `Frame.from_bytes()` remains
   strict. Compute the same CRC via `binascii.crc_hqx`, including arbitrary
   16-bit initial states. Default client/factory response matching is command
   **and** sequence; each retry gets a fresh sequence. Check echoed stream/file
   selectors where present, and deliver attitude telemetry even if it also
   completes a query. ACK flags alone do not decide matching. Explicit
   `response_matching="command"` keeps compatibility with uncorrelated devices,
   but cannot reject delayed responses with the same command ID. There is no
   automatic downgrade.
4. **Video memory and lifecycle (3–5 days).** Prune FPS timestamps on insertion
   and reset on restart; expose stopped/starting/running/reconnecting/stopping/
   failed states plus `last_error`. Serialize start/stop, clean failed startup,
   and wake consumers when producers end. Native backends deliver one replaceable
   latest-frame slot with at most one loop notification. Join workers off-loop,
   retain ownership on a failed 5-second shutdown, and block restart until an
   old worker exits. OpenCV requests 3-second native open/read timeouts where
   supported. Reconnect delay doubles from the configured start up to 30 seconds,
   resets after 30 healthy seconds, and obeys attempt limits for open/read loss.
5. **A8 aiortsp (5–8 days).** Use explicit aiortsp connection, media session,
   and TCP/UDP RTP transports without changing the URL. Select H.264 payload,
   clock, mode, and SPS/PPS from SDP. Handle single NAL, STAP-A, and FU-A in
   modes 0/1; reject interleaved/H.265. Deduplicate, reorder up to 64 packets
   or 50 ms, discard damaged units, and resume on a usable keyframe. Bound
   combined queued/reorder storage at 1,024 packets or 4 MiB and access units
   at 8 MiB. Decode complete units serially in one PyAV worker, reconnect after
   a five-second decoded-frame stall, and cancel/await session tasks on teardown.
   AUTO requires a decoded frame within five seconds before accepting a backend;
   startup failure cleans that candidate and tries the next. Explicit backend
   selection reports its failure directly.
6. **GStreamer, logging, measurements (2–4 days).** Map BGR/BGRx using actual
   plane offset/stride into an owned array. Filter disabled logs before rendering,
   only build hex output in trace mode, and log routine ACKs at DEBUG. Compare
   deterministic baseline/final runs and record platform, Python and dependency
   versions, workload, and logging configuration.

## Per-finding estimate and impact

**Measured** refers to an observation; **calculated** uses the stated workload.
Savings can overlap and must not be added. Reliability impacts are acceptance
outcomes, not estimates of field failure rates.

| Finding | Implemented change | Estimated saving or impact |
|---|---|---|
| B01 | Lazy video exports and complete optional dependencies | Base installation imports without NumPy. Control-only startup/RSS savings require measurement. |
| B02 | Reconnect after EOF/reset | An affected disconnect is retried rather than leaving a dead reader. First retry begins after 0.5 s plus connection/replay time. |
| B03 | One supervised recovery path | Avoids duplicate readers, stale parser data, dead heartbeat. Detected loss fails requests promptly instead of using the 6.3 s default retry budget. |
| B04 | Exception-safe close | Closes owned transport and clears readiness after task failure; no credible CPU saving. |
| B05 | Recover within mixed chunks | Valid neighbors remain deliverable; can avoid the 2 s timeout for each otherwise discarded ACK. Link corruption frequency is unknown. |
| B06 | Distinct FC/gimbal registries | Preserves equal-valued enum subscriptions and accurate replay/unsubscribe; negligible performance effect. |
| B07 | Sequence plus echoed-selector matching | Rejects stale/mismatched replies in strict mode and still delivers telemetry; field error rate unknown. |
| B08 | Cancellation cleanup | Zero stale pending entries; prevents the next push from being consumed by an abandoned future. |
| B09 | Isolated callback dispatch | Remaining subscribers receive valid updates after an exception/self-unsubscribe; slight snapshot overhead. |
| B10 | Bounded rolling FPS history | At 30 fps and no FPS reads, roughly 3.3 MiB of retained timestamps per hour avoided; retains about one second. Calculated from observed object sizes. |
| B11 | Explicit stream lifecycle | False-running states disappear and failed producers can restart; recovery time remains workload-specific. |
| B12 | Complete A8 H.264 aiortsp path | Supports fragmented/aggregated RTP and selected TCP/UDP; necessary decoding work may add CPU. H.265 excluded. |
| B13 | Nonblocking native join | Avoids up to 5 s of event-loop blocking per join. Prior 200 ms fake join delayed a 10 ms timer to 201 ms. |
| B14 | Latest-frame coalescing | Thirty 1080p BGR frames occupy about 178 MiB; one slot can avoid about 172 MiB of one-second scheduled backlog, excluding decoder/consumer buffers. |
| B15 | Thermal work excluded | Out of scope for A8. No runtime saving claimed. |
| B16 | Stride-aware GStreamer copy | Correct padded/custom pipeline frames. Aligned A8 dimensions may be unchanged; no speedup expected. |
| P01 | Native CRC | Prior Windows/MSYS microbenchmark: 2.2755 s → 0.0256 s for 1,000 × 4,096 B, about 89× faster / 98.9% less checksum time; not video decoding. |
| P02 | Bulk parser | Planning target 2× parser throughput, previously unmeasured. Overall 100 Hz control CPU gain likely modest. |
| P03 | Early log filter, ACK at DEBUG | At 100 ACK/s and assumed 200 B lines, about 20 kB/s or 72 MB/hour of INFO output removed. CPU benefit must be measured. |
| P04 | Notification-driven consumers | About ten idle timeout cycles/s per consumer removed, or 30/s for three. Queue waits already wake on data, so no assumed 100 ms active latency improvement. |

## Validation gates

- Installation: clean base wheel import without NumPy, plus isolated imports
  for each available stream extra; validate wheel and sdist builds.
- Client: EOF/reset/heartbeat/replay failure, cancellation at send/response/
  retry/lock boundaries, repeated close and close during reconnect, exactly
  one active reader/heartbeat, no obsolete pending request or owned transport.
- Protocol: CRC vectors/random initial states, sequence wrap, delayed/duplicate
  replies, wrong selectors, telemetry during query, valid/corrupt/valid under
  every two-way fragmentation boundary.
- Streaming: concurrent start/stop, finite/exhausted producers, five-second
  stall, callbacks, native-shutdown delay, one-hour 30 fps timestamps, and one
  notification across a blocked-loop thirty-frame backlog.
- RTSP: local TCP and UDP sessions with recorded synthetic H.264; verify SDP
  parameter sets, FU-A/STAP-A, loss/reorder/duplicate/wrap, decoded dimensions
  and image color through real PyAV. GStreamer padded BGR/BGRx and aligned
  1920×1080 copies are checked independently where PyGObject is unavailable.
- Quality and performance: functional suite, lint/type checks, clean builds
  on supported Python versions when available. Compare identical baseline/final
  inputs with five-run medians for CPU, retained memory, frame age, command
  p50/p95/p99 and timer delay. Target synthetic shutdown timer p99 <20 ms.
  Benchmarks write to an explicit untracked output, never the tracked baseline.

**Physical A8 gate still open:** test actual ACK/sequence semantics, attitude
pushes during queries, reconnect, zoom-stop fallback, recording feedback, and
RTSP TCP/UDP on the camera. Until those pass, A8 hardware compatibility is
not verified.

## Measured results

Measurements use `scripts/benchmark_sdk.py` against an untouched pre-change
source snapshot in `.tools/baseline`, and `scripts/measure_import.py` for
fresh-process imports. They use five runs and report medians. Platform:
Windows 11, CPython 3.12.10 (64-bit), NumPy 2.2.6, OpenCV 5.0.0.93,
aiortsp 1.4.0, PyAV 18.1.0, structlog 25.5.0. Logging was WARNING with
trace disabled. The synthetic control/video workload sent 100 echo commands
at 100 Hz alongside 30 preallocated 1080p BGR frames at 30 fps; it did not
decode RTSP or include camera/network latency. CPU time resolution and
scheduling on this Windows host limit small-difference interpretation.

| Measurement | Baseline | Implemented | Interpretation |
|---|---:|---:|---|
| Control-only package import | 893 ms; 66.60 MiB RSS | 650 ms; 49.42 MiB RSS | 243 ms (27%) and 17.18 MiB (26%) lower; NumPy absent from final import. |
| CRC, 1,000 × 4,096-byte buffers | 0.670 s | 0.0125 s | 53.8× throughput, 98.1% less checksum time on this host. |
| Parser, 1 MiB of 64-byte-payload frames in one feed | 0.78 MiB/s | 26.66 MiB/s | 34.2× throughput; with 1 KiB chunks, 0.76 → 27.68 MiB/s (36.5×). |
| Synthetic concurrent workload, process CPU | 0.0469 s | 0.0313 s | A small, noisy difference; not a predicted production CPU saving. |
| Synthetic concurrent workload, traced peak allocation | 0.0295 MiB | 0.0256 MiB | Preallocated video image excluded; not evidence of real decode memory use. |
| Synthetic frame age p50 / p95 | 0.0268 / 0.0348 ms | 0.0216 / 0.0374 ms | No consistent latency improvement. |
| Synthetic command latency p50 / p95 / p99 | 1.281 / 1.945 / 2.404 ms | 0.336 / 0.630 / 0.894 ms | Local echo only, not camera command latency. |
| 10 ms timer delay p99 during 200 ms native join | 156.14 ms | 14.10 ms | Meets the <20 ms synthetic shutdown target; maximum delay 191.23 → 15.73 ms. |
| 30 fps timestamps after simulated hour without reading FPS | 108,000 | 31 | Rolling history retains about one second. |
| Traced peak during 30 × 1080p frame burst with blocked loop | 178.02 MiB | 11.87 MiB | 166.15 MiB less peak allocation in this fixture; final peak includes transient producer frames. |

The isolated wheel check imported base control with NumPy blocked and each
tested OpenCV/aiortsp backend with the other backend's dependency blocked.
PyGObject/GStreamer runtime and Linux/Jetson builds could not be exercised on
this Windows host; GStreamer plane mapping was tested with synthetic samples.
No physical A8 Mini was available, so ACK/sequence behavior and end-to-end
video/control compatibility remain unverified. These savings overlap and
must not be summed.

The Windows Python 3.12 run passed **806 tests** (21 skipped) with **90.78%**
coverage, above the repository's 90% gate. Ruff, Black, strict mypy,
wheel/sdist builds, and isolated wheel imports passed. Python 3.10, 3.11,
and 3.13 checks are configured in CI but were not run locally.
