# Using the SDK

This guide covers `SIYIClient`, which talks to the camera directly from the computer wired to it. To control the camera from a ground station, use the camera service and `RemoteCamera` instead; see the [ground station guide](remote.md). `RemoteCamera` has the same command methods, return types and exceptions, so most of this guide applies to it too. The differences:

| `SIYIClient` (direct) | `RemoteCamera` (ground station, over RPC) |
| --- | --- |
| `connect_udp()` / `connect_tcp()` / `connect_serial()` | `RemoteCamera("ws://<companion-ip>:8765")` |
| `rotate()` / `rotate_nowait()`, `manual_zoom()` | `rate()`, `zoom_rate()`: setpoints that stop on their own |
| `request_gimbal_stream()` + `on_attitude()` | `on_attitude()` (the service runs the stream) |
| `GimbalPointLock` in your process | `lock()` / `release()`, running on the aircraft |
| Errors from the camera | Same errors, plus `NotControllerError`, `ServiceUnavailableError`, … |

The [web dashboard](WEB_UI.md) is a bench and debugging tool built on the same API; nothing here depends on it.

Everything is `asyncio`. Every camera command is a coroutine on one `SIYIClient`.

## Install

```bash
pip install -e .                    # control (UDP/TCP/UART) and the media HTTP API
pip install -e ".[stream-opencv]"   # + RTSP video through OpenCV
pip install -e ".[tracking]"        # + point lock (OpenCV, NumPy)
```

The [README](../README.md) lists the other video backends. Python 3.10-3.13.

## Connecting

```python
import asyncio
from siyi_sdk import connect_udp

async def main() -> None:
    async with await connect_udp("192.168.144.25") as camera:
        print(await camera.get_firmware_version())

asyncio.run(main())
```

| Helper | Default target | `auto_reconnect` default | Notes |
| --- | --- | --- | --- |
| `connect_udp(ip, port=37260)` | `192.168.144.25:37260` | `False` | No heartbeat needed |
| `connect_tcp(ip, port=37260)` | `192.168.144.25:37260` | `True` | Sends the required 1 Hz heartbeat itself |
| `connect_serial(device, baud=115200)` | e.g. `/dev/ttyUSB0` | `True` | UART |

All three accept `timeout` (default per-command reply timeout, 2 s), `max_retries` (2) and `auto_reconnect`, and return a connected client. `async with` closes it on exit; without it, call `await camera.close()` yourself.

To pick the transport yourself, build the client directly:

```python
from siyi_sdk import SIYIClient
from siyi_sdk.transport.tcp import TCPTransport

camera = SIYIClient(TCPTransport(ip="192.168.144.25", port=37260),
                    default_timeout=1.0, max_retries=1, auto_reconnect=True)
await camera.connect()
```

Use **one client per camera per process**. The client serializes requests that share a command ID, matches replies by command ID, and runs one reader task; a second client on the same link would see the first one's replies.

### Reconnection

With `auto_reconnect=True`, a lost transport is reopened after 0.5, 1, 2, 4 and 8 s. Gimbal data streams you requested (`request_gimbal_stream`) are requested again, and your `on_attitude` / `on_function_feedback` callbacks stay registered. While reconnecting, and after all five attempts fail, commands raise `NotConnectedError`. `camera.connection_event` (an `asyncio.Event`) is set while the link is up, so a supervisor can watch it:

```python
if not camera.connection_event.is_set():
    ...  # report "camera link down" to the operator
```

Requests in flight when the link drops fail with `ConnectionError`.

## Commands

Methods are grouped below; each has a docstring with units and ranges. Enums live in `siyi_sdk.models`.

| Area | Methods |
| --- | --- |
| System | `get_firmware_version`, `get_hardware_id`, `set_utc_time`, `soft_reboot`, `heartbeat` |
| Gimbal | `rotate`, `set_attitude`, `set_single_axis`, `one_key_centering`, `get_gimbal_mode`, `get_gimbal_attitude`, `get_magnetic_encoder` |
| Zoom (digital) | `absolute_zoom`, `manual_zoom`, `get_zoom_range`, `get_current_zoom` |
| Camera | `capture`, `get_camera_system_info`, `get_encoding_params`, `set_encoding_params`, `format_sd_card`, `get_osd_flag`, `set_osd_flag` |
| Telemetry and autopilot | `request_gimbal_stream`, `on_attitude`, `on_function_feedback`, `send_aircraft_attitude`, `send_raw_gps` |
| Video | `create_stream` (see [streaming](streaming.md)) |
| Media files | `MediaClient` (separate HTTP client; see [media HTTP API](media_http_api.md)) |

```python
from siyi_sdk.models import CaptureFuncType, CenteringAction

await camera.set_attitude(yaw_deg=30.0, pitch_deg=-45.0)   # absolute angles (0x0E)
await camera.rotate(yaw=50, pitch=0)                        # speed, -100..100 (0x07)
await camera.rotate(0, 0)                                   # stop
await camera.one_key_centering(CenteringAction.CENTER)
await camera.absolute_zoom(3.0)                             # 1.0 up to the resolution's limit
await camera.capture(CaptureFuncType.PHOTO)
await camera.capture(CaptureFuncType.LOCK_MODE)             # gimbal modes are set through capture()
```

Gimbal motion modes (Lock, Follow, FPV) are selected with `capture(CaptureFuncType.LOCK_MODE | FOLLOW_MODE | FPV_MODE)` and read back with `get_gimbal_mode()`. Recording toggles with `CaptureFuncType.START_RECORD`.

A8 Mini specifics that change what a command does:

- Zoom is digital. The maximum depends on the recording resolution: 720p 6.0x, 1080p 5.5x, 1440p 3.5x, 4K 1.0x. `absolute_zoom` checks this and raises `ConfigurationError` instead of sending a command the camera would never answer.
- `set_encoding_params` reads the settings back and raises `ResponseError` if the camera ignored them (it acknowledges success regardless). Applying them resets zoom to 1x.
- `format_sd_card` waits up to 60 s because the camera only replies once formatting has finished. It is never retried.
- `capture()`, `send_aircraft_attitude()` and `send_raw_gps()` get no reply from the camera; they return once sent. Photo and recording results arrive through `on_function_feedback`.

### Acknowledged vs. fire-and-forget

Most commands wait for the camera's reply. That costs a round trip, and requests with the same command ID queue behind each other. For setpoints you send continuously, use the `_nowait` variants, which send and return:

| Waits for reply | Fire-and-forget |
| --- | --- |
| `rotate` | `rotate_nowait` |
| `set_attitude` | `set_attitude_nowait` |
| `set_single_axis` | `set_single_axis_nowait` |
| `manual_zoom` | `manual_zoom_nowait` |

Use the acknowledged form for one-off commands where you want an error if the camera didn't answer. Use `_nowait` for joystick or control-loop traffic at 10-100 Hz, and use the attitude stream (below) as feedback. The reply to `set_attitude` only reports the attitude when the command arrived, not that the target was reached.

Angle setpoints are never retried, so a late retry cannot drive the gimbal to an old target. Idempotent reads (firmware version, attitude, zoom, encoding, and so on) and centering are retried up to `max_retries` with exponential backoff.

### Unsupported commands

The client also has methods for vendor commands the A8 Mini firmware (camera v0.3.7 / gimbal v0.4.9) never answers. They raise `UnsupportedCommandError` immediately without sending anything: `get_ip_config`, `set_ip_config`, `get_system_time`, `get_gimbal_system_info`, `request_fc_stream`, `get_picture_name_type`, `set_picture_name_type`, `get_control_mode`, `get_weak_threshold`, `set_weak_threshold`, `get_motor_voltage`, `get_weak_control_mode`, `set_weak_control_mode`. The camera's IP address can only be changed with SIYI's own tools.

## Telemetry

The camera pushes gimbal attitude once you request a stream. Register a callback first, then request a rate:

```python
from siyi_sdk.models import DataStreamFreq, GimbalAttitude, GimbalDataType

def on_attitude(att: GimbalAttitude) -> None:
    print(att.yaw_deg, att.pitch_deg, att.roll_deg, att.yaw_rate_dps)

unsubscribe = camera.on_attitude(on_attitude)
await camera.request_gimbal_stream(GimbalDataType.ATTITUDE, DataStreamFreq.HZ50)
...
await camera.request_gimbal_stream(GimbalDataType.ATTITUDE, DataStreamFreq.OFF)
unsubscribe()
```

Rates are `HZ2` to `HZ100`. The A8 Mini reports yaw relative to the aircraft body.

`on_function_feedback(cb)` reports camera events as `FunctionFeedback` values: `PHOTO_OK`, `PHOTO_FAILED`, `RECORDING_STARTED`, `RECORDING_STOPPED`, `RECORDING_FAILED`.

Callbacks are **synchronous and run on the client's reader task**. Keep them short: record the value, or hand it to your own task, and don't block or `await` inside them. A slow callback delays every reply behind it. Exceptions raised in a callback are logged and swallowed.

```python
latest: GimbalAttitude | None = None
updates: asyncio.Queue[GimbalAttitude] = asyncio.Queue(maxsize=1)

def on_attitude(att: GimbalAttitude) -> None:
    global latest
    latest = att                      # newest value for anyone who polls
    if updates.full():
        updates.get_nowait()          # keep only the newest for the consumer task
    updates.put_nowait(att)
```

Magnetic-encoder and motor-voltage streams (`GimbalDataType.MAGNETIC_ENCODER`, `MOTOR_VOLTAGE`) can be requested too, but their pushes are currently only logged at DEBUG; there is no subscription API for them.

Send flight-controller data to the gimbal with `send_aircraft_attitude(AircraftAttitude(...))` and `send_raw_gps(RawGPS(...))`.

## Errors

Every SDK exception derives from `siyi_sdk.exceptions.SIYIError`:

```
SIYIError
├── TransportError
│   ├── ConnectionError       link failed or dropped mid-request
│   ├── TimeoutError          no reply within the timeout (after retries)
│   ├── SendError
│   └── NotConnectedError     closed, or reconnecting
├── CommandError
│   ├── NACKError             camera rejected the command
│   ├── UnsupportedCommandError   A8 Mini firmware has no handler; nothing was sent
│   └── ResponseError         camera replied with failure, or ignored the settings
├── ProtocolError             framing/CRC/payload problems (logged and skipped by the reader)
└── ConfigurationError        argument out of range; raised before sending
```

`siyi_sdk.exceptions.TimeoutError` and `ConnectionError` shadow the built-ins of the same name; import them under an alias if you also handle the built-ins:

```python
from siyi_sdk.exceptions import ConfigurationError, SIYIError
from siyi_sdk.exceptions import TimeoutError as CameraTimeout

try:
    await camera.absolute_zoom(zoom)
except ConfigurationError as exc:
    reject(str(exc))                  # bad input: tell the caller, don't retry
except CameraTimeout:
    report("camera did not answer")   # UDP replies do get lost; retry if it is safe
except SIYIError as exc:
    report(f"camera error: {exc}")
```

Per-call overrides exist where they matter: `get_camera_system_info(timeout=..., max_retries=...)` and `format_sd_card(timeout=...)`.

## Video

```python
stream = camera.create_stream()        # RTSP from the client's camera IP, backend AUTO
stream.on_frame(lambda frame: handle(frame.frame, frame.timestamp))
await stream.start()                   # returns after the first decoded frame
...
await stream.stop()
```

`StreamFrame.frame` is a BGR NumPy array and `timestamp` is the arrival time on the `time.monotonic()` clock. The picture is typically 150-300 ms old by then. See [streaming](streaming.md) for backends and `SIYIStream` options.

## Logging

The SDK logs through `structlog`. Call `configure_logging()` once at startup:

```python
from siyi_sdk import configure_logging

configure_logging()                  # INFO, console format
configure_logging(fmt="json")        # one JSON object per line, for journald or log shipping
configure_logging(trace=True)        # DEBUG plus hex dumps of every frame
```

The environment variables `SIYI_LOG_LEVEL`, `SIYI_LOG_FORMAT` and `SIYI_PROTOCOL_TRACE=1` set the same options.

## Running inside a long-lived service

The SDK's camera service already follows these rules; see the [ground station guide](remote.md). If you build your own network front end instead, follow them too:

- **One owner.** Create the client once at startup and share it. Don't open a client per request or per ground-station connection.
- **Stop on silence.** A speed command (`rotate`, `rotate_nowait`) keeps the gimbal turning until the next one arrives. If speed commands come from a remote operator, send `rotate_nowait(0, 0)` when their stream stops (for example, no command for 300 ms) or their connection drops. Angle targets don't have this problem: the gimbal stops at the target.
- **Rate-limit at the edge.** Forward the newest setpoint at a fixed rate (20-50 Hz) with `_nowait` instead of awaiting one acknowledged command per incoming message. Otherwise a burst of messages queues up behind replies.
- **Keep closed loops local.** Anything that reacts to video, such as point lock, must run next to the camera. The extra network delay to a ground station would make it oscillate. Send the loop's inputs (for example, "lock on pixel x, y"), not its outputs.
- **Report link state.** Watch `connection_event`, and forward attitude and function feedback as events instead of making the ground station poll.
- **Fail requests cleanly.** Map `ConfigurationError` to "bad request", `UnsupportedCommandError` to "not available on this camera", and `TimeoutError` / `NotConnectedError` to "camera unavailable".

## Testing without a camera

`siyi_sdk.transport.mock.MockTransport` stands in for the network. Queue the reply frames you expect, then check what the client sent:

```python
from siyi_sdk import SIYIClient
from siyi_sdk.constants import CMD_REQUEST_HARDWARE_ID
from siyi_sdk.protocol.frame import Frame
from siyi_sdk.transport.mock import MockTransport

transport = MockTransport()
camera = SIYIClient(transport, default_timeout=0.1)
await camera.connect()
transport.queue_response(
    Frame(ctrl=1, seq=1, cmd_id=CMD_REQUEST_HARDWARE_ID, data=bytes(12)).to_bytes()
)
hardware = await camera.get_hardware_id()
assert transport.sent_frames                 # raw bytes written by the client
await camera.close()
```

The tests under `tests/` contain many more examples.

## Further reading

- [Point lock](tracking.md): keep the gimbal on a fixed spot using video, with delay compensation.
- [Firmware tracking](firmware-tracking.md): the camera's built-in tracking controller (experimental).
- [Ground station guide](remote.md): the camera service and `RemoteCamera` over RPC.
- [Streaming](streaming.md), [media HTTP API](media_http_api.md), [protocol notes](protocol.md).
- [Examples](../examples/README.md).
