# SIYI SDK overall review

Reviewed: 2026-09-27. Repository snapshot: `b02cc5f`; SDK version: `0.6.0`.

This reviews the current SDK as a whole, rather than a recent diff. Scope includes the client, protocol parser/framing/CRC, command codecs and models, UDP/TCP/serial transports, HTTP media client, all three video backends, packaging, and relevant tests. The web server and examples were inspected for SDK usage and downstream impact; this is not a full browser UI or security audit.

The highest-priority problems are base installation/import failure, broken connection recovery, incomplete cleanup, lost frames during parser recovery, ambiguous response routing, and the aiortsp decoding path. There are **16 bug findings** below: 7 high priority and 9 medium priority. Four additional performance opportunities follow. Priority reflects user impact, not security severity.

## Evidence and limitations

- Source review used the bundled `SIYI_SDK_PROTOCOL.txt` as the device protocol reference. Command layouts checked against it included system time, attitude, GPS, thermal measurements, stream control, and AI tracking.
- Isolated checks executed actual SDK code using Python 3.12.7 at `C:\msys64\mingw64\bin\python.exe`. A no-op `structlog` module and a minimal NumPy module were injected because dependencies were unavailable. No real arrays or camera hardware were used. These checks establish Python control-flow behavior, not end-to-end compatibility with the installed libraries.
- Base import was checked before injecting the NumPy stub and failed with `No module named 'numpy'` after bypassing the unavailable logging dependency.
- The existing pytest suite **was not run successfully**: `python` was absent from PATH, and the discovered interpreter had neither pytest nor pip. No claim is made that existing tests pass or fail.
- Real TCP/UDP/serial devices, RTSP sessions, GStreamer buffers, and HTTP responses were not exercised. Findings based on these integrations are explicitly marked as source/API analysis.
- No SDK implementation or existing test files were changed. Only this report was added.

## Bugs

### B01 — High: base SDK installation can fail on import

**Location:** [siyi_sdk/__init__.py:16](siyi_sdk/__init__.py#L16), [stream/models.py:18](siyi_sdk/stream/models.py#L18), [pyproject.toml:26](pyproject.toml#L26).

Importing `siyi_sdk` eagerly imports the streaming package, whose models unconditionally import NumPy. NumPy is absent from the base dependency list. Consequently a clean `pip install .` does not supply everything needed even for `from siyi_sdk import connect_udp`. The `stream-gst` extra also does not explicitly supply NumPy. The isolated import reproduced the missing-NumPy exception.

**Recommendation:** keep video exports lazy and place NumPy in each streaming dependency set, or deliberately make NumPy a declared base dependency. Add clean-environment import checks for the base wheel and each extra. The Hatch test environment also lacks NumPy while `tests/stream/conftest.py` imports it directly; check that environment independently of development dependencies.

### B02 — High: ordinary TCP/serial disconnects bypass automatic reconnect

**Location:** [client.py:431](siyi_sdk/client.py#L431), [transport/tcp.py:124](siyi_sdk/transport/tcp.py#L124), [transport/serial.py:133](siyi_sdk/transport/serial.py#L133).

TCP and serial `stream()` handle EOF and common OS errors by setting `_connected=False` and ending iteration. The client invokes `_reconnect()` only when iteration raises an exception. Normal exhaustion therefore ends the reader permanently even with `auto_reconnect=True`.

**Reproduced:** an EOF-emulating transport had only one `connect()` call, a completed reader task, and a still-set connection event after disconnect.

**Recommendation:** handle unexpected iterator termination as connection loss; distinguish it from intentional shutdown. Fail outstanding requests promptly and start one supervised recovery path. Test graceful EOF as well as reset/error cases.

### B03 — High: reconnect does not restore a clean client lifecycle

**Location:** [client.py:547](siyi_sdk/client.py#L547), [client.py:530](siyi_sdk/client.py#L530).

**Source analysis:** `_reconnect()` opens the transport and starts a reader but does not close the previous transport, reset the parser, fail/clear requests from the old connection, or restart a failed heartbeat. A heartbeat failure itself only ends its task; it does not initiate recovery. After a reconnectable reader exception, a dead heartbeat can remain dead, and partial old-session bytes can contaminate the new session. Subscription replay failure can trigger another connection attempt while the newly created reader is still active.

**Recommendation:** centralize disconnect/reconnect supervision. Stop previous reader/heartbeat tasks, release the old connection, reset parser state, fail old-session requests, then start one reader and heartbeat for the new session. Roll back a failed replay before retrying. Test reconnect with a partial frame, pending requests, failed heartbeat, and failed subscription replay.

### B04 — High: failed background tasks can prevent resource cleanup

**Location:** [client.py:279](siyi_sdk/client.py#L279).

`close()` awaits heartbeat and reader tasks while suppressing only `CancelledError`. If a task has already failed, awaiting it raises its original exception. Execution never reaches pending-request cleanup or `transport.close()`.

**Reproduced:** queueing a reader `ValueError`, waiting for the task to fail, then calling `close()` raised that error and left the mock transport connected. Separately, successful `close()` leaves `connection_event` set, so consumers can observe a stale connection signal.

**Recommendation:** perform cleanup in `finally` blocks, collect/log task failures, and clear the connection event on loss and close. Cleanup must complete even when a background task has already failed.

### B05 — High: one corrupt frame discards valid neighboring frames

**Location:** [protocol/parser.py:113](siyi_sdk/protocol/parser.py#L113), especially lines 188 and 241; [client.py:438](siyi_sdk/client.py#L438).

`feed()` raises immediately on an oversized length or bad CRC. It loses both the locally accumulated valid frames and the unprocessed remainder of the input chunk. The client catches the exception and skips the chunk. Resetting parser state does not recover those discarded bytes.

**Reproduced:** `feed(valid + corrupt + valid)` raised `CRCError`; a subsequent empty feed returned no frames. Neither valid neighbor was delivered. This is particularly relevant to TCP/serial, where multiple frames can share a read.

**Recommendation:** return successfully parsed frames while recording malformed-frame errors separately, and continue scanning remaining bytes. Preserve partial trailing frames. Include valid/corrupt/valid and oversized-header/valid cases in one feed, plus fragmented versions of both.

### B06 — Medium: FC and gimbal subscriptions overwrite each other

**Location:** [client.py:226](siyi_sdk/client.py#L226), [client.py:908](siyi_sdk/client.py#L908), [client.py:925](siyi_sdk/client.py#L925).

The shared `_active_streams` dictionary uses two different `IntEnum` classes as keys. Members with the same integer value compare and hash equally. FC attitude and gimbal attitude are both `1`; FC RC channels and gimbal laser are both `2`.

**Reproduced:** requesting FC attitude at 10 Hz followed by gimbal attitude at 5 Hz left one entry: an `FCDataType` key with the gimbal frequency. Reconnect would replay the wrong command. Turning off the gimbal stream then removed the FC entry too.

**Recommendation:** use separate registries or keys such as `(command_id, data_type)`. Test both command families together, including independent disable and replay.

### B07 — High: any frame with the same command ID can complete a request

**Location:** [client.py:455](siyi_sdk/client.py#L455), [client.py:318](siyi_sdk/client.py#L318).

Pending responses are matched solely by command ID. The dispatcher ignores sequence number, ACK status, and request-specific echoed fields. Per-command locking prevents simultaneous waiters, but cannot distinguish a late response to the previous request from the current response. It also consumes unsolicited attitude/laser frames instead of delivering them to subscribers when a query for that command is pending.

**Reproduced:** a pending `0x20` request for stream `2` completed with a frame having `CTRL=0`, sequence `65535`, and unrelated payload. No correlation checks ran.

**Recommendation:** validate sequence/ACK information where supported and check echoed stream/file/coordinate fields where applicable. Preserve push delivery. Hardware compatibility needs verification before enforcing strict ACK/sequence rules: existing tests intentionally build responses with several CTRL values. Devices that do not echo sequences need an explicit documented fallback rather than implicit acceptance of every same-command frame.

### B08 — Medium: cancelled requests leave entries in the pending registry

**Location:** [client.py:378](siyi_sdk/client.py#L378), [client.py:423](siyi_sdk/client.py#L423).

Cancellation is not caught by `except Exception` on supported Python versions. Cancelling a request during its response wait leaves its cancelled future in `_pending` until another frame, request, or close removes it. The dispatcher removes the stale entry and returns even if the future is already done; for telemetry commands this can discard the next push.

**Reproduced:** cancelling an in-flight command left `[1]` in the pending registry.

**Recommendation:** clean up in `finally`, removing the entry only if it still refers to this request's future. Test cancellation during send, response wait, retry backoff, and lock acquisition.

### B09 — Medium: one telemetry subscriber can suppress other subscribers

**Location:** [client.py:482](siyi_sdk/client.py#L482).

All callbacks for a push run inside one shared `try` block over the live callback list. An exception in the first callback skips every later callback. A callback that unsubscribes itself modifies the list during iteration and skips the next callback too.

**Reproduced:** both a raising first subscriber and a self-unsubscribing first subscriber caused the second attitude subscriber to receive zero calls.

**Recommendation:** iterate over a snapshot and isolate errors per callback, as the video dispatcher already does. Document that telemetry callbacks must be fast; blocking work here also delays command ACK processing.

### B10 — Medium: FPS bookkeeping grows without bound

**Location:** [stream/stream.py:68](siyi_sdk/stream/stream.py#L68), [stream/stream.py:214](siyi_sdk/stream/stream.py#L214), [stream/stream.py:235](siyi_sdk/stream/stream.py#L235).

Every decoded frame appends a timestamp to an unbounded deque. Old timestamps are removed only when application code reads `fps`. Applications that only register callbacks or use `last_frame` accumulate timestamps for the entire stream lifetime.

**Reproduced:** 10,000 generated frames left 10,000 timestamps with `maxlen=None`. At 30 fps this means 108,000 retained timestamps per hour. The eventual first `fps` read also has to prune the entire backlog synchronously.

**Recommendation:** prune by age on insertion, retain only the rolling window, and reset statistics on restart. Verify a long stream without any `fps` access.

### B11 — Medium: a finished video loop still reports itself as running

**Location:** [stream/stream.py:134](siyi_sdk/stream/stream.py#L134), [stream/stream.py:235](siyi_sdk/stream/stream.py#L235), [stream/opencv_backend.py:146](siyi_sdk/stream/opencv_backend.py#L146).

Video-loop exhaustion or an exception leaves `_running=True`. Calling `start()` again then returns immediately instead of starting a new loop. Backend reconnect limits do not consistently terminate consumers either: OpenCV can exit its capture thread after failed opens without setting its stop event or notifying its queue consumer. Its reconnect limit is checked only in the failed-open branch, not after repeated successful opens followed by failed reads.

**Reproduced:** a finite fake backend left a completed task with `is_running=True`; calling `start()` created no new task. The OpenCV limit/consumer behavior is source analysis.

**Recommendation:** explicitly represent stopped/starting/streaming/failed states, notify consumers when producers exit, and clean up when the loop terminates. Enforce reconnect limits consistently. Test exhaustion, exception, repeated read failures, and restart after failure.

### B12 — High: aiortsp backend lacks a working general RTSP decoding path

**Location:** [stream/aiortsp_backend.py:120](siyi_sdk/stream/aiortsp_backend.py#L120), especially lines 128–137; [stream/stream.py:109](siyi_sdk/stream/stream.py#L109).

**Source/API analysis; no live decoding test:**

- The SDK passes each raw RTP payload directly to the H.264 decoder. It does not depacketize fragmented or aggregated NAL units or apply SDP parameter sets. An RTP fragment is not a complete decoder input. This conclusion follows from the [upstream reader implementation](https://raw.githubusercontent.com/marss/aiortsp/master/aiortsp/rtsp/reader.py) and [H.264 RTP packetization specification](https://datatracker.ietf.org/doc/html/rfc6184#section-5.8).
- Decoder creation is hardcoded to `h264`, ignoring the accepted `codec='h265'` configuration.
- `StreamConfig.transport` is ignored. Upstream chooses UDP for `rtsp:` and TCP for `rtspt:`/`rtsps:`, so the SDK's default plain `rtsp:` URL does not implement its default TCP setting. See [upstream transport selection](https://raw.githubusercontent.com/marss/aiortsp/master/aiortsp/transport/__init__.py).
- `except av.AVError` is incompatible with allowed PyAV versions starting at 14, where that alias was removed. On a decode exception, looking up the missing exception class can itself raise `AttributeError`. See the [PyAV 14 changelog](https://raw.githubusercontent.com/PyAV-Org/PyAV/v14.0.0/CHANGELOG.rst).

Also verify the reader import against supported installed versions: the SDK calls `aiortsp.RTSPReader`, while the [documented import](https://github.com/marss/aiortsp) is `from aiortsp.rtsp.reader import RTSPReader`.

AUTO selects this backend based on package availability, so these failures can prevent an otherwise available OpenCV backend from being used.

**Recommendation:** use a tested RTSP demux/decode implementation or implement depacketization and SDP setup fully. Respect codec/transport, use the supported exception API, and add real-library decoding tests with fragmented H.264 and H.265 fixtures. Availability-only tests cannot validate this path.

### B13 — Medium: asynchronous video shutdown blocks the event loop

**Location:** [stream/opencv_backend.py:99](siyi_sdk/stream/opencv_backend.py#L99), [stream/gstreamer_backend.py:207](siyi_sdk/stream/gstreamer_backend.py#L207).

Both async disconnect methods call `Thread.join(timeout=5.0)` synchronously. A blocked capture/supervisor thread therefore blocks all asyncio work for up to five seconds, including control commands, heartbeat tasks, and timeout handling. OpenCV drops its thread reference even if the join times out; the old worker can continue running, and reconnecting the same backend clears its shared stop event.

**Reproduced:** a fake thread that took 200 ms to join delayed an independent 10 ms asyncio timer to 201 ms. Real capture shutdown duration was not measured.

**Recommendation:** join outside the event loop, use bounded native capture open/read timeouts where supported, and verify the worker has exited before releasing its reference or restarting. An asyncio `wait_for()` cannot enforce a deadline while the event loop is blocked inside synchronous join.

### B14 — Medium: video delivery keeps stale frames and can retain an unbounded callback backlog

**Location:** [stream/opencv_backend.py:240](siyi_sdk/stream/opencv_backend.py#L240), [stream/gstreamer_backend.py:425](siyi_sdk/stream/gstreamer_backend.py#L425).

When the size-one asyncio queue is full, both backends discard the newly decoded frame and keep the older one. This contradicts the stated latest-frame behavior. More significantly, every frame schedules a `call_soon_threadsafe` callback retaining that frame before the bounded queue is checked. A slow or blocked event loop can accumulate many full images in its callback queue despite `maxsize=1`.

**Reproduced:** executing each backend's actual `_put` body with an old queued frame and a new incoming frame retained `old`. The callback-backlog risk is source analysis; its memory growth rate was not measured.

**Recommendation:** maintain a latest-frame mailbox and coalesce notifications so at most one delivery callback is outstanding. Replace a stale queued frame with the newer frame. Track dropped frames and delivery age under slow-consumer tests.

### B15 — Medium: continuous thermal measurements have no delivery API

**Location:** [client.py:1155](siyi_sdk/client.py#L1155), [client.py:1170](siyi_sdk/client.py#L1170), [client.py:1186](siyi_sdk/client.py#L1186), `_STREAM_PUSH_CMDS` near [client.py:155](siyi_sdk/client.py#L155).

**Source/protocol analysis:** public temperature methods accept `TempMeasureFlag.CONTINUOUS_5HZ`, but commands `0x12`, `0x13`, and `0x14` are absent from the push dispatcher and have no subscription callbacks. The first response can satisfy the request; subsequent measurements become unexpected frames and are discarded. Stream configuration for these measurements is also not retained for reconnect.

**Recommendation:** provide temperature subscription/cache APIs and track continuous measurement settings, or explicitly reject continuous mode until it is supported. Verify disabling measurement against hardware as well; the bundled protocol does not establish whether disable always produces a measurement-shaped ACK.

### B16 — Medium: GStreamer buffer conversion assumes tightly packed rows

**Location:** [stream/gstreamer_backend.py:396](siyi_sdk/stream/gstreamer_backend.py#L396).

**Conditional source/API finding:** the mapped buffer is reshaped directly into `(height, width, channels)`. GStreamer frames can include row padding and plane offsets, described by [GstVideoInfo](https://gstreamer.freedesktop.org/documentation/video/video-info.html). With padded BGR rows, the byte count does not match the reshape dimensions, or extra layout assumptions are invalid. The handler logs the failure and emits no frame.

Typical aligned camera resolutions may avoid this, but custom pipelines are supported and can produce padded layouts. For example, a 641-pixel BGR row with 1,924-byte stride has padding that this reshape cannot represent.

**Recommendation:** map with video metadata and construct an array using the actual offset and stride, then copy the visible pixels. Test padded BGR and BGRx samples. No real padded GStreamer buffer was tested here.

## Performance improvements

### P01 — Replace the Python CRC loop with the native equivalent

**Location:** [protocol/crc.py:23](siyi_sdk/protocol/crc.py#L23).

`binascii.crc_hqx(buf, init)` implements the checksum needed here without a Python iteration per byte. The existing public function can retain its name/signature while delegating internally.

Measured on the discovered interpreter, using 1,000 iterations over a 4,096-byte buffer:

| Implementation | Elapsed |
| --- | ---: |
| Existing table loop | 2.2755 seconds |
| `binascii.crc_hqx` | 0.0256 seconds |

That is approximately **89× faster for this CRC microbenchmark**, not an estimate of overall SDK speedup. Equivalence passed the `123456789 -> 0x31C3` vector, 257 deterministic buffers, and 1,000 random buffers up to 4,096 bytes with random 16-bit initial states. Keep protocol golden-vector coverage when changing the implementation.

### P02 — Parse headers and payloads in bulk after fixing recovery

**Location:** [protocol/parser.py:113](siyi_sdk/protocol/parser.py#L113).

The state machine repeatedly evaluates Python enum branches for every byte, appends payload bytes to two buffers, and copies them again for checksum/model creation. A buffered parser can find STX, unpack the fixed header, check whether the complete frame is available, and process payload slices in bulk. Keep an offset and compact periodically rather than repeatedly deleting from the front of a large buffer.

The checked-in [benchmark result](tests/benchmarks/results.json) reports 2.697 MB/s; it is historical data, not a new parser measurement. This is already ample for ordinary control telemetry, so prioritize reliability and profile on the target device before a parser rewrite. Re-run fragmented/corrupt-stream properties and compare small-frame latency as well as throughput.

### P03 — Avoid formatting disabled logs on high-rate paths

**Location:** [transport/udp.py:151](siyi_sdk/transport/udp.py#L151), [transport/tcp.py:110](siyi_sdk/transport/tcp.py#L110), [transport/serial.py:120](siyi_sdk/transport/serial.py#L120), [logging_config.py:122](siyi_sdk/logging_config.py#L122), [client.py:397](siyi_sdk/client.py#L397).

Transport calls build `data.hex()` even when DEBUG output is disabled. The configured structlog processor chain also lacks an early `filter_by_level`, so rendering work can happen before the underlying logger rejects the event. Every successful command additionally emits an INFO ACK message, which can dominate terminal/disk logging during high-rate polling.

Add early level filtering, generate hex only in enabled trace paths, and consider DEBUG or sampling for routine ACKs. Measure throughput and event-loop responsiveness with INFO/WARNING/trace modes; no logging speedup was measured here.

### P04 — Use explicit wakeups instead of 100 ms idle polling

**Location:** [transport/udp.py:164](siyi_sdk/transport/udp.py#L164), [stream/opencv_backend.py:127](siyi_sdk/stream/opencv_backend.py#L127), [stream/gstreamer_backend.py:249](siyi_sdk/stream/gstreamer_backend.py#L249).

Each consumer repeatedly creates a `wait_for(queue.get(), timeout=0.1)` wait and handles timeout exceptions while idle. This creates roughly ten idle timeout cycles per second per consumer. A close/failure sentinel or explicit notification lets the consumer block until data or shutdown arrives, and also supports the producer-exit fix in B11.

This is a modest efficiency improvement. The current polling does not impose a 100 ms delay on frames that arrive while the consumer is waiting.

## Test gaps and suggested fix order

1. Add a clean base-wheel import check and independently install each extra. Restore connection supervision and exception-safe shutdown first (B01–B04).
2. Add mixed corrupt/valid chunk tests, simultaneous FC/gimbal stream replay, stale replies, cancellation, and self-unsubscribing callbacks (B05–B09). Existing parser recovery tests feed the recovery frame in a separate call, which misses B05.
3. Add sustained streaming without `fps` reads, producer exhaustion, reconnect limits, slow consumers, and shutdown responsiveness (B10–B14). Test with the actual optional libraries, including a known decodable RTP recording.
4. Add continuous thermal delivery and padded GStreamer buffer coverage (B15–B16), then evaluate the measured CRC improvement and remaining performance suggestions.

Several shared protocol fixtures are stale: `frame_system_time_ack` at `tests/conftest.py:488` builds an 8-byte calendar payload rather than the 12-byte timestamp payload; `frame_get_ip_ack` at line 598 builds only 4 bytes rather than 12; `frame_ai_track_stream_push` at line 547 builds 12 bytes rather than 10. Searches found these fixtures only at their definitions, so they do not demonstrate current runtime failures. Repair or remove them before reusing them for regression tests.

The parser benchmark describes a 50 MB/s goal while actually asserting 1 MB/s (`tests/benchmarks/test_parser_throughput.py:93`). Make its stated goal and enforced threshold consistent and record interpreter/platform details. Hardware-specific performance thresholds should run separately from functional tests.

## Minimal reproductions in an environment with SDK dependencies installed

These snippets demonstrate selected defects without camera hardware. They intentionally observe the current incorrect behavior.

```python
# B05: valid frames on both sides are lost when feed() raises.
from siyi_sdk.protocol.frame import Frame
from siyi_sdk.protocol.parser import FrameParser
from siyi_sdk.exceptions import CRCError

wire = Frame(ctrl=2, seq=1, cmd_id=1, data=b"12345678").to_bytes()
bad = wire[:-1] + bytes([wire[-1] ^ 1])
parser = FrameParser()
try:
    parser.feed(wire + bad + wire)
except CRCError:
    print(parser.feed(b""))  # []: no valid neighbor remains available
```

```python
# B06: distinct protocol families collide as dictionary keys.
from siyi_sdk.models import FCDataType, GimbalDataType, DataStreamFreq

active = {
    FCDataType.ATTITUDE: DataStreamFreq.HZ10,
    GimbalDataType.ATTITUDE: DataStreamFreq.HZ5,
}
print(len(active))  # 1, although two independent streams were requested
```

```python
# B04: a reader failure interrupts close() before transport cleanup.
import asyncio
from siyi_sdk.client import SIYIClient
from siyi_sdk.transport.mock import MockTransport

async def reproduce():
    transport = MockTransport()
    client = SIYIClient(transport)
    await client.connect()
    transport.queue_error(ValueError("reader failed"))
    await asyncio.sleep(0.05)
    try:
        await client.close()
    except ValueError:
        print(transport.is_connected)  # True
    finally:
        await transport.close()  # release the resource for this reproduction

asyncio.run(reproduce())
```
