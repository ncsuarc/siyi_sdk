# A8 Mini quickstart

The camera's default address is `192.168.144.25`. Control uses port `37260` (UDP or TCP), video uses RTSP on port `8554`, and the media file API uses HTTP on port `82`. Put the computer that talks to the camera on the `192.168.144.x` network, for example at `192.168.144.20`.

## 1. Check the camera directly

On the computer wired to the camera:

```bash
pip install -e .
```

```python
import asyncio
from siyi_sdk import connect_udp

async def main():
    async with await connect_udp("192.168.144.25") as camera:
        print(await camera.get_hardware_id())
        print(await camera.get_gimbal_attitude())
        await camera.absolute_zoom(2.0)

asyncio.run(main())
```

If that times out, check that you can ping the camera and that your network interface is on the `192.168.144.x` subnet.

`connect_tcp()` and `connect_serial()` cover the A8 Mini's other control inputs; TCP heartbeat is automatic. Zoom is digital, and optical focus commands are not part of this SDK.

## 2. Control it from a ground station

On the companion computer wired to the camera, start the camera service:

```bash
pip install -e ".[remote-server]"
python -m siyi_sdk.remote.server --camera-ip 192.168.144.25
```

On the ground station, which must reach the companion computer on port 8765:

```bash
pip install -e ".[remote]"
```

```python
import asyncio
from siyi_sdk.remote import RemoteCamera

async def main():
    async with RemoteCamera("ws://<companion-ip>:8765") as cam:
        print(await cam.get_state())
        await cam.set_attitude(0.0, -30.0)

asyncio.run(main())
```

`get_state()` shows `"alive": true` under `camera` once the service hears the camera. If it stays false, run step 1 on the companion computer. The [ground station guide](remote.md) covers the rest: joystick control, point lock, events and errors.

## Next

- [Setting up Iris and Shimmer](deployment.md): our aircraft and ground station, step by step.
- [Ground station guide](remote.md): `RemoteCamera` and the RPC protocol.
- [SDK guide](sdk-guide.md): `SIYIClient` in your own program.
- [Streaming](streaming.md) and [examples](../examples/README.md).
- To try commands interactively on the bench, use the [debugging dashboard](WEB_UI.md) (`pip install -e ".[web]"`, then `python -m web_ui.server`). It has a command explorer and a protocol log.
