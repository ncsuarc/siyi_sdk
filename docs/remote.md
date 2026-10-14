# Ground station control over RPC

In flight, the A8 Mini is wired to a companion computer (for example a Raspberry Pi), and the ground station reaches that computer over the radio link. The SDK runs on the companion computer as a **camera service**. The ground station controls the camera through **remote procedure calls** (JSON-RPC 2.0 over one websocket), using the Python client `RemoteCamera`:

| Connection | Port | Carries |
| --- | --- | --- |
| Ground station → companion computer | TCP 8765 (websocket) | Commands, joystick setpoints and events, as JSON-RPC |
| Companion computer → A8 Mini | UDP 37260 | SIYI camera protocol |
| Ground station → A8 Mini | RTSP 8554 | Live video, straight from the camera |

- **Companion computer:** the service owns the camera. Point lock runs here, because its control loop can't tolerate radio latency. This is also where manual motion is stopped if commands stop arriving.
- **Ground station:** `RemoteCamera` has the same method names, arguments, return types and exceptions as the local `SIYIClient`, plus joystick setpoints, point lock and pushed events. Other languages can speak the [JSON protocol](#wire-format) directly.
- **Video:** the ground station decodes `rtsp://<camera>:8554/main.264` itself, so the camera's network must be reachable from the ground. The service does not relay video. The same applies to the [media HTTP API](media_http_api.md) on port 82.

## Quick start

**1. On the companion computer**, install and start the service:

```bash
pip install -e ".[remote-server]"
python -m siyi_sdk.remote.server --camera-ip 192.168.144.25
```

It listens on port 8765 and keeps retrying until the camera answers.

From the ground, check what the service sees:

```bash
python -m siyi_sdk.remote status ws://192.168.144.30:8765
```

**2. On the ground**, install the client and connect:

```bash
pip install -e ".[remote]"
```

```python
import asyncio
from siyi_sdk.models import CaptureFuncType
from siyi_sdk.remote import RemoteCamera

async def main() -> None:
    async with RemoteCamera("ws://192.168.144.30:8765", name="gcs") as cam:
        print(await cam.get_firmware_version())
        cam.on_attitude(lambda att: print(att.yaw_deg, att.pitch_deg))

        await cam.set_attitude(0.0, -45.0)
        await cam.capture(CaptureFuncType.PHOTO)

        cam.rate(40, 0)                 # turn right while held
        await asyncio.sleep(1)
        cam.rate(0, 0)                  # stop

        await cam.lock(0.0, 0.0)        # hold the spot at the image centre
        await asyncio.sleep(10)
        await cam.release()

asyncio.run(main())
```

Replace `192.168.144.30` with the companion computer's address. [`examples/remote_ground.py`](../examples/remote_ground.py) is a complete script. For our aircraft, [Setting up Iris and Shimmer](deployment.md) has the step-by-step install, boot service and checks.

## Using `RemoteCamera`

### Connecting

```python
cam = RemoteCamera(
    "ws://192.168.144.30:8765",
    name="gcs",            # shown to other ground stations
    timeout=5.0,           # default reply timeout, seconds
    take_control=True,     # ask for control on every (re)connect if nobody has it
    reconnect=True,        # reconnect after the link drops
)
await cam.connect()        # or: async with RemoteCamera(...) as cam:
...
await cam.close()          # stops held setpoints, then disconnects
```

`cam.connection_event` is set while connected. After a drop, the client reconnects with backoff (0.5 s up to 5 s) and asks for control again. While disconnected, calls raise `NotConnectedError`, and in-flight calls fail with `ConnectionError`.

### Camera commands

These behave like the `SIYIClient` methods of the same name: same arguments, the same `siyi_sdk.models` return types, the same exceptions.

| Area | Methods |
| --- | --- |
| System | `get_firmware_version()`, `get_hardware_id()`, `set_utc_time(unix_usec)`, `soft_reboot(camera=, gimbal=, confirm=True)` |
| Gimbal | `set_attitude(yaw_deg, pitch_deg)`, `set_single_axis(axis, angle_deg)`, `one_key_centering(action)`, `get_gimbal_mode()`, `get_gimbal_attitude()`, `get_magnetic_encoder()`, and the `_nowait` setpoint variants |
| Zoom | `absolute_zoom(zoom)`, `get_zoom_range()`, `get_current_zoom()` |
| Camera | `capture(func)`, `get_camera_system_info()`, `get_encoding_params(stream)`, `set_encoding_params(params)`, `format_sd_card(confirm=True)`, `get_osd_flag()`, `set_osd_flag(on)` |
| Autopilot data | `send_aircraft_attitude(att)`, `send_raw_gps(gps)` |

```python
from siyi_sdk.models import CaptureFuncType, CenteringAction

await cam.set_attitude(30.0, -20.0)
await cam.one_key_centering(CenteringAction.CENTER)
await cam.absolute_zoom(3.0)
await cam.capture(CaptureFuncType.START_RECORD)
await cam.capture(CaptureFuncType.LOCK_MODE)        # gimbal motion mode
await cam.format_sd_card(confirm=True)               # destructive: needs confirm
```

A camera that doesn't answer raises `TimeoutError` exactly as it would locally. A timeout with `cmd_id=-1` means the *service* didn't reply in time.

Anything not wrapped can be called by name with `await cam.call("method_name", **params)`.

### Moving with a joystick

Speed control uses **setpoints**, not commands:

```python
cam.rate(yaw, pitch)        # 0x07 speeds, -100..100; (0, 0) stops
cam.zoom_rate(direction)    # 1 in, -1 out, 0 stop
await cam.stop()            # stop both now (acknowledged)
```

Call `rate()` whenever the stick moves; there is nothing to await. While a setpoint is non-zero, the client resends it every 100 ms. The service stops the gimbal if no update arrives for 0.3 s, or the moment the controlling ground station disconnects. A frozen program or lost radio link therefore stops the gimbal on its own.

The SDK's raw speed commands (`rotate`, `manual_zoom` and their `_nowait` variants) are not available remotely, because they keep the gimbal moving until another command arrives.

Manual rate input releases a running point lock. So do `set_attitude`, `set_single_axis`, `one_key_centering`, `set_encoding_params` and `soft_reboot`. Zooming does not.

### Point lock

Point lock keeps the gimbal on a fixed spot in the scene while the aircraft moves. It runs on the companion computer; the ground station only starts and stops it and watches its status.

```python
cam.on_lock_status(print)                       # 10 Hz while locked, plus every state change

status = await cam.lock(x, y)                   # start; returns the first status
await cam.lock(x, y, controller="rate")         # pick a controller for this lock
status = await cam.get_lock()
final = await cam.release()                     # stop; returns the final status
```

- **Coordinates** are normalized offsets from the image centre: `x` from -0.5 (left edge) to 0.5 (right edge), `y` from -0.5 (top) to 0.5 (bottom). Convert a click on a `W`×`H` video widget with `x = px / W - 0.5` and `y = py / H - 0.5`.
- **`controller`** chooses how the lock steers: `"angle"` (0x0E angle targets, the default), `"rate"` (the app's 0x07 speed loop), or `"firmware"` (the camera's own tracking controller; [experimental](firmware-tracking.md)). The stored default is used when omitted.
- **Status** dicts carry `state` (`"locked"`, `"searching"` or `"idle"`), `controller`, `score`, the spot's position `x`/`y` in the same normalized units, and, for app locks, `error_deg` and the commands sent. When a lock ends by itself (tracking lost, video stopped, manual override), the idle status says why in `reason`.

**Lock on the frame the operator clicked.** The ground shows video later than the aircraft receives it. If the gimbal is moving, the newest frame on the aircraft no longer shows what the operator clicked. Pass the clicked frame's arrival time:

```python
frame = ground_stream.last_frame                        # the frame on screen
await cam.lock(x, y, frame_time=frame.timestamp)        # StreamFrame.timestamp, ground clock
```

`RemoteCamera` pings the service every 2 s and converts that time to the service's clock. The service picks the matching frame from its last second of video. It also subtracts `ground_extra_video_delay_ms`, which is how much later the ground's own RTSP picture arrives than the aircraft's. That setting defaults to 100 ms and should be measured on the real link. `cam.rtt` and `cam.clock_offset` show the current estimate.

**Calibrate once per installation.** The lock steers best with measured delays and axis directions:

```bash
python -m siyi_sdk.remote calibrate ws://192.168.144.30:8765
```

or from code, `await cam.calibrate()`. The gimbal turns about 20 degrees per axis and back, which takes about 10 s.

Point the camera at a textured, still scene first. The results are saved on the companion computer and reused after reboots. Measure once per mounting (upright or upside down); both are remembered.

**Settings:**

```python
config = await cam.get_lock_config()
await cam.set_lock_config(control="rate", response=0.8)   # saved on the companion computer
learned = await cam.call("get_learned_loop")               # what rate locks learned about the motors
await cam.call("apply_learned_loop")                       # fold that into the calibration
```

Useful settings: `control` (the default controller), `response` (gain multiplier; lower is gentler), `max_speed`, `model` (`"local"` for ground targets, `"global"` for distant ones), `hfov_deg`, and `ground_extra_video_delay_ms`. The rest are calibration results. See [point lock](tracking.md) for what they mean.

### Telemetry and events

The service pushes events; register callbacks instead of polling:

| Callback | Receives | When |
| --- | --- | --- |
| `on_attitude(cb)` | `GimbalAttitude` | 20 Hz by default (`--attitude-hz`) |
| `on_function_feedback(cb)` | `FunctionFeedback` | Photo taken or failed, recording started or stopped |
| `on_lock_status(cb)` | dict (see above) | 10 Hz while locked, and on every state change |
| `on_link(cb)` | `{"camera": bool, "video": bool, "source": "firmware" \| "rtsp" \| None, "firmware_tracking": bool}` | Camera or video comes or goes; `{"service": False}` when the connection to the service drops |
| `on_zoom(cb)` | `{"zoom": float, "max": float}` | Zoom level changes |
| `on_control(cb)` | `{"controller": bool, "holder": name}` | Control changes hands |

Each returns an unsubscribe function. Callbacks run on the client's reader task, so keep them short.

`await cam.get_state()` returns everything at once: camera (alive, firmware, attitude, mount, zoom), video, held setpoints, lock status and control.

### Control

The first ground station to connect becomes the **controller**. Others are **observers**: they receive every event and may call read-only methods (`get_*`, `get_state`, `ping`). Their writes raise `NotControllerError`, and their setpoints are ignored.

```python
cam.is_controller                   # updated live
await cam.take_control()            # if nobody has it
await cam.take_control(force=True)  # take it from another ground station
await cam.release_control()
```

If the controller disconnects, manual motion stops immediately. A running lock continues by default; start the service with `--on-disconnect release` to stop it too. The next ground station to connect, or to call `take_control()`, becomes the controller.

### Errors

Errors arrive as the same exception types a local `SIYIClient` raises, plus a few from the service. All derive from `siyi_sdk.exceptions.SIYIError`.

| Exception | Meaning |
| --- | --- |
| `ConfigurationError` | Bad arguments, or `confirm=True` missing on `format_sd_card` / `soft_reboot` |
| `TimeoutError` | The camera didn't answer (`cmd_id=-1`: the service didn't answer) |
| `ConnectionError`, `NotConnectedError` | Camera link down, or not connected to the service |
| `UnsupportedCommandError`, `NACKError`, `ResponseError` | As for the local client |
| `NotControllerError` | Another ground station has control |
| `ServiceUnavailableError` | Not possible right now, e.g. a lock with no live video |
| `ConflictError` | A calibration is running, or the mounting differs from the stored calibration |
| `OperationFailedError` | The calibration ran but didn't fit; nothing was changed |
| `UnknownMethodError`, `RemoteError` | No such method, or an unexpected failure in the service |

The service-specific ones are importable from `siyi_sdk.remote`.

## Command line

```bash
python -m siyi_sdk.remote status    ws://<host>:8765   # camera, video, lock and control as the service sees them
python -m siyi_sdk.remote calibrate ws://<host>:8765   # measure the point-lock loop (turns the gimbal)
python -m siyi_sdk.remote release   ws://<host>:8765   # stop a point lock
```

The command exits with status 1 and prints the error if the service can't be reached.

## Running the service

```bash
python -m siyi_sdk.remote.server --camera-ip 192.168.144.25 --port 8765
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--camera-ip` | `192.168.144.25` | Camera address |
| `--host`, `--port` | `0.0.0.0`, `8765` | Where to listen |
| `--config` | `~/.config/siyi/lock.json` | Lock settings and calibration, saved whenever they change |
| `--video` | `auto` | Tracking video: `auto` uses the camera's lower-latency firmware link when the firmware supports it, otherwise RTSP. Also `rtsp`, `firmware`, `both`, or `none` (no point lock) |
| `--on-disconnect` | `keep-lock` | `release` also stops a running lock when the controller disconnects |
| `--setpoint-timeout` | `0.3` | Seconds without a setpoint update before manual motion stops |
| `--attitude-hz` | `20` | Rate of attitude events sent to the ground |
| `--log-format` | `json` | `json` for journald, `console` for a terminal |

To start the service at boot, add `/etc/systemd/system/siyi-camera.service`:

```ini
[Unit]
Description=SIYI camera service
After=network-online.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/siyi_sdk
ExecStart=/home/pi/siyi_sdk/.venv/bin/python -m siyi_sdk.remote.server --camera-ip 192.168.144.25
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

Then run `sudo systemctl enable --now siyi-camera` and follow the logs with `journalctl -u siyi-camera -f`.

To run the service inside your own program instead of the CLI:

```python
from siyi_sdk.remote import CameraServer, CameraSession

session = CameraSession("192.168.144.25", config_path="lock.json")
async with CameraServer(session, port=8765):
    await asyncio.Event().wait()     # serve until cancelled
```

`CameraSession` also works without the server, as an in-process API for the same features (`set_rate`, `start_lock`, `calibrate`, `subscribe`, …).

**Security:** the websocket has no authentication or encryption. Anyone who can reach the port can move the gimbal. Keep it on the aircraft's private radio network, or tunnel it, for example over WireGuard.

## RPC reference

**Controller** marks methods that only the controlling ground station may call.

### Service methods

| Method | Params | Result | Controller |
| --- | --- | --- | --- |
| `hello` | `name?` | `{protocol, sdk, t, controller}` | |
| `ping` | | `{t}`, the service clock | |
| `get_state` | | camera, video, motion, lock and control | |
| `list_commands` | | each camera command with a JSON schema | |
| `take_control` | `force?` | `{controller: true}` | |
| `release_control` | | `{controller: false}` | |
| `stop` | | `{stopped: true}` | ✓ |
| `lock` | `x, y, frame_time?, controller?, size?` | lock status | ✓ |
| `release` | `reason?` | final lock status | ✓ |
| `get_lock` | | lock status | |
| `calibrate` | | `{config, notes, fit_error_deg}` | ✓ |
| `get_lock_config` | | settings | |
| `set_lock_config` | any settings | settings | ✓ |
| `get_learned_loop` | | `{ready, samples, ...}` | |
| `apply_learned_loop` | | `{config, applied}` | ✓ |

### Camera commands

Each takes the same named parameters as the `SIYIClient` method and returns its result. `get_*` commands are open to observers; the rest need control.

`get_firmware_version`, `get_hardware_id`, `set_utc_time {unix_usec}`, `soft_reboot {camera, gimbal, confirm}`, `one_key_centering {action}`, `set_attitude {yaw_deg, pitch_deg}`, `set_attitude_nowait {yaw_deg, pitch_deg}`, `set_single_axis {axis, angle_deg}`, `set_single_axis_nowait {axis, angle_deg}`, `get_gimbal_mode`, `get_gimbal_attitude`, `get_magnetic_encoder`, `absolute_zoom {zoom}`, `get_zoom_range`, `get_current_zoom`, `get_camera_system_info`, `capture {func}`, `get_encoding_params {stream}`, `set_encoding_params {params}`, `format_sd_card {timeout?, confirm}`, `get_osd_flag`, `set_osd_flag {on}`, `send_aircraft_attitude {att}`, `send_raw_gps {gps}`.

### Setpoints (ground → service, no reply)

| Method | Params | Notes |
| --- | --- | --- |
| `rate` | `yaw, pitch` (-100..100) | Resend at least every 0.3 s while moving; `0, 0` stops |
| `zoom_rate` | `direction` (1, -1, 0) | Same |

### Events (service → ground)

`attitude {attitude, t}`, `feedback {feedback, t}`, `lock_status {state, ..., t}`, `link {camera, video, source, firmware_tracking}`, `zoom {zoom, max}`, `control {controller, holder}`. `t` is the service's `time.monotonic()`.

## Wire format

Each websocket text message is one JSON-RPC 2.0 object. This is all a client in another language needs.

```jsonc
// request → reply
{"jsonrpc": "2.0", "id": 7, "method": "set_attitude", "params": {"yaw_deg": 10, "pitch_deg": -30}}
{"jsonrpc": "2.0", "id": 7, "result": {"$type": "SetAttitudeAck", "yaw_deg": 9.8, "pitch_deg": -29.9, "roll_deg": 0.1}}

// setpoint: no id, no reply
{"jsonrpc": "2.0", "method": "rate", "params": {"yaw": 40, "pitch": 0}}

// event from the service
{"jsonrpc": "2.0", "method": "lock_status", "params": {"state": "locked", "controller": "angle", "score": 0.81, "x": 0.02, "y": -0.01, "t": 5123.4}}

// error
{"jsonrpc": "2.0", "id": 8, "error": {"code": -32000, "message": "Timeout waiting for response to command 0x0D after 2.0s",
  "data": {"kind": "camera_timeout", "type": "TimeoutError", "fields": {"cmd_id": 13, "timeout_s": 2.0}}}}
```

- **Values:** SDK dataclasses arrive as `{"$type": "<model name>", ...fields}`, enums as `{"$enum": "<name>", "name": "PHOTO", "value": 0}`, and bytes as `{"$bytes": "<base64>"}`. Arguments may be plain JSON, so `{"func": 0}` works for `capture`.
- **Errors:** `error.data.kind` is one of `bad_args`, `unsupported`, `camera_timeout`, `camera_offline`, `rejected`, `not_controller`, `unavailable`, `conflict`, `failed`, `unknown_method` or `internal`.
- **Keepalive:** both ends send websocket pings every second and drop the connection after 3 s without a reply.

A minimal client without `RemoteCamera`, using only the `websockets` package:

```python
import asyncio, json
from websockets.asyncio.client import connect

async def main() -> None:
    async with connect("ws://192.168.144.30:8765") as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "set_attitude",
                                  "params": {"yaw_deg": 0, "pitch_deg": -45}}))
        async for text in ws:
            msg = json.loads(text)
            if msg.get("id") == 1:
                print(msg.get("result") or msg.get("error"))
                break
            # anything without an id is an event: attitude, link, control, ...

asyncio.run(main())
```
