# SIYI A8 Mini Python SDK

Async Python control and live video for the SIYI A8 Mini gimbal camera. It covers the SIYI Gimbal Camera External SDK Protocol for the commands the A8 Mini supports, RTSP video, the camera's media HTTP API, and video-based point lock.

There are two ways to use it:

- **Directly**, in a program on the computer wired to the camera: `SIYIClient` talks to the camera over UDP, TCP or UART.
- **From a ground station over RPC**: the SDK runs as a camera service on the aircraft's companion computer (for example a Raspberry Pi). The ground station controls it through `RemoteCamera`, which has the same methods as `SIYIClient`. Point lock and the safety stop run on the aircraft.

```
ground station ── RemoteCamera ── websocket (JSON-RPC) ──▶ companion computer ── SIYIClient ──▶ A8 Mini
```

A local web dashboard is included for bench testing and debugging.

## Install

```bash
pip install -e .                         # control over UDP, TCP, UART, and the media API
pip install -e ".[remote-server]"        # camera service on the companion computer
pip install -e ".[remote]"               # ground-station client for that service
pip install -e ".[stream-opencv]"        # live video through OpenCV
pip install -e ".[stream-gst]"           # live video through GStreamer (system libraries required)
pip install -e ".[stream-aiortsp]"       # live video through aiortsp + PyAV
pip install -e ".[tracking]"             # point lock (OpenCV, NumPy)
pip install -e ".[web]"                  # debugging dashboard
```

Python 3.10-3.13.

## From a ground station (RPC)

On the companion computer:

```bash
python -m siyi_sdk.remote.server --camera-ip 192.168.144.25      # listens on port 8765
```

On the ground:

```python
import asyncio
from siyi_sdk.models import CaptureFuncType
from siyi_sdk.remote import RemoteCamera

async def main():
    async with RemoteCamera("ws://192.168.144.30:8765") as cam:   # the companion computer
        cam.on_attitude(lambda att: print(att.yaw_deg, att.pitch_deg))

        await cam.set_attitude(0.0, -45.0)        # same call as SIYIClient.set_attitude
        await cam.capture(CaptureFuncType.PHOTO)

        cam.rate(40, 0)                           # joystick: turns while held, stops if the link drops
        await asyncio.sleep(1)
        cam.rate(0, 0)

        await cam.lock(0.1, -0.05)                # point lock runs on the aircraft
        await asyncio.sleep(10)
        await cam.release()

asyncio.run(main())
```

To set it up on our machines (Iris on the aircraft, Shimmer on the ground), follow **[Setting up Iris and Shimmer](docs/deployment.md)**. The **[ground station guide](docs/remote.md)** covers the full API: joystick setpoints, point lock and calibration, events, control between several ground stations, errors, running the service at boot, and the JSON protocol for clients in other languages.

## Directly

```python
import asyncio
from siyi_sdk import connect_udp
from siyi_sdk.models import CaptureFuncType, DataStreamFreq, GimbalDataType

async def main():
    async with await connect_udp("192.168.144.25") as camera:
        print(await camera.get_firmware_version())

        camera.on_attitude(lambda att: print(att.yaw_deg, att.pitch_deg))
        await camera.request_gimbal_stream(GimbalDataType.ATTITUDE, DataStreamFreq.HZ10)

        await camera.set_attitude(yaw_deg=20.0, pitch_deg=-30.0)
        await camera.absolute_zoom(2.0)              # digital zoom
        await camera.capture(CaptureFuncType.PHOTO)
        await asyncio.sleep(2)

asyncio.run(main())
```

`connect_tcp()` and `connect_serial()` work the same way; TCP sends its required heartbeat automatically. The **[SDK guide](docs/sdk-guide.md)** covers connection options and reconnection, acknowledged vs. fire-and-forget commands, telemetry callbacks, errors, logging, and testing without a camera.

### Live video

```python
stream = camera.create_stream()          # RTSP for this camera's IP
stream.on_frame(lambda frame: print(frame.width, frame.height))
await stream.start()
...
await stream.stop()
```

The A8 Mini has one RTSP URL, `rtsp://192.168.144.25:8554/main.264`. `SIYIStream(StreamConfig(build_rtsp_url(host)))` builds a stream without a control client, which is how a ground station shows video. `StreamBackend.AUTO` tries GStreamer, then aiortsp, then OpenCV. See [streaming](docs/streaming.md).

## What's included

| Area | Where |
| --- | --- |
| Ground-station control over RPC | `siyi_sdk.remote`; [ground station guide](docs/remote.md) |
| System, gimbal, zoom, camera and telemetry commands | `SIYIClient`; [SDK guide](docs/sdk-guide.md) |
| RTSP video | `SIYIStream`; [streaming](docs/streaming.md) |
| Photos and videos on the SD card (HTTP, port 82) | `MediaClient`; [media HTTP API](docs/media_http_api.md) |
| Hold a fixed spot in view while the aircraft moves | `siyi_sdk.tracking`; [point lock](docs/tracking.md) |
| The camera's built-in tracking controller (experimental) | `FirmwareTrackingClient`; [firmware tracking](docs/firmware-tracking.md) |
| Protocol details and A8 Mini quirks | [protocol notes](docs/protocol.md) |

The A8 Mini has digital zoom only; there are no focus, thermal or rangefinder commands. Some vendor commands have no handler in A8 Mini firmware, and their methods raise `UnsupportedCommandError` (see [protocol notes](docs/protocol.md)). The [vendor protocol](SIYI_SDK_PROTOCOL.pdf) covers the whole SIYI range, including commands for hardware the A8 Mini lacks.

## Debugging dashboard

```bash
pip install -e ".[web]"
python -m web_ui.server
```

Open `http://localhost:8082` for live video, manual gimbal and camera controls, a command explorer, and a protocol log. It is a bench tool for checking the camera and the SDK, not the ground-station interface. See [web UI](docs/WEB_UI.md).

## Examples

[examples/](examples/README.md) has small scripts for each area, including `remote_ground.py` for the ground-station client.

## License

MIT; see [LICENSE](LICENSE).
