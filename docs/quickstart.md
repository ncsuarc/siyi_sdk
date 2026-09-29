# A8 Mini quickstart

The default camera address is `192.168.144.25`. Control uses port `37260`; video uses RTSP port `8554`.

```bash
pip install -e ".[web]"
python -m web_ui.server
```

Open `http://localhost:8082` to use the live dashboard, select a video backend, and try supported SDK commands in the command explorer. The camera must be powered and reachable on your network.

For Python:

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

`connect_tcp()` and `connect_serial()` support the A8 Mini's other control inputs. TCP heartbeat is automatic. A8 Mini zoom is digital; optical focus commands are not part of this SDK.

SIYI defines `SEQ` as a frame sequence but does not require an ACK to repeat the request's sequence. The A8 Mini trace showed independent counters, so the SDK matches replies by command ID by default on UDP, TCP, and UART. Pass `response_matching="sequence"` only if your firmware echoes request sequences and you need strict matching. The web dashboard also matches by command ID. See the [official SIYI protocol](https://siyi.biz/siyi_file/A8%20mini/SIYI_Gimbal_Camera_External_SDK_Protocol_Update_Log%20V0.1.1.pdf).

See [examples](../examples/README.md), [streaming](streaming.md), and [web UI](WEB_UI.md).
