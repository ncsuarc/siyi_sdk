# SIYI A8 Mini Python SDK

Async Python control and live video for the SIYI A8 Mini gimbal camera. The SDK uses the SIYI Gimbal Camera External SDK Protocol; this repository exposes commands relevant to the A8 Mini and an optional local web dashboard.

## Install

```bash
pip install -e .                         # UDP, TCP, UART control and media API
pip install -e ".[stream-opencv]"         # OpenCV live video
pip install -e ".[stream-gst]"            # GStreamer live video (system libraries required)
pip install -e ".[stream-aiortsp]"        # aiortsp + PyAV live video
pip install -e ".[web]"                   # Web dashboard with live video
```

The video backends can coexist. `StreamBackend.AUTO` tries GStreamer, then aiortsp, then OpenCV until one produces a decoded frame. The A8 Mini RTSP URL is `rtsp://192.168.144.25:8554/main.264`. It has no separate RTSP sub stream; `StreamType.SUB` remains available for encoding settings used by SIYI's separate private video path.

## Connect and control

```python
import asyncio
from siyi_sdk import connect_udp
from siyi_sdk.models import CaptureFuncType

async def main():
    async with await connect_udp("192.168.144.25", 37260) as camera:
        print(await camera.get_firmware_version())
        print(await camera.get_gimbal_attitude())
        await camera.absolute_zoom(2.0)  # digital zoom
        await camera.capture(CaptureFuncType.PHOTO)

asyncio.run(main())
```

`connect_tcp()` and `connect_serial()` are also available. TCP sends its required heartbeat automatically; UDP and UART do not need heartbeat packets. The A8 Mini has digital zoom, but autofocus and manual focus commands for optical zoom cameras are unavailable here.

## Live video

```python
import asyncio
from siyi_sdk import SIYIStream, StreamConfig, build_rtsp_url

async def main():
    stream = SIYIStream(StreamConfig(build_rtsp_url()))
    stream.on_frame(lambda frame: print(frame.width, frame.height))
    await stream.start()
    try:
        await asyncio.sleep(10)
    finally:
        await stream.stop()

asyncio.run(main())
```

See [streaming](docs/streaming.md) and the [examples](examples/README.md) for the OpenCV, GStreamer, and combined control paths. Backend availability depends on installed Python and system packages.

## Web dashboard and command explorer

```bash
pip install -e ".[web]"
python -m web_ui.server
```

Open `http://localhost:8082`. The dashboard provides live video, gimbal and camera controls, media browsing, backend selection, and a typed command explorer for the retained A8 Mini SDK methods. The explorer shows camera replies and recent capture feedback. Formatting the SD card, rebooting, and changing the device IP require confirmation. See [web UI details](docs/WEB_UI.md).

## API groups

- System, firmware, time, IP configuration, and reboot
- Gimbal modes, target angles, velocity, centering, and attitude telemetry
- Digital zoom, photo/video capture, encoding, SD card, media, HDMI/CVBS, and OSD
- Flight-controller attitude/GPS data and ArduPilot diagnostics

The [protocol reference](SIYI_SDK_PROTOCOL.pdf) is the vendor-wide specification; it includes commands for hardware that the A8 Mini does not have. See [A8 Mini quickstart](docs/quickstart.md) for supported connection patterns.

## License

MIT; see [LICENSE](LICENSE).
