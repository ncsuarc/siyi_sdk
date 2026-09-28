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

Some A8 Mini firmware replies use a sequence counter independent of requests. `connect_udp()` therefore matches replies by command ID by default. Pass `response_matching="sequence"` if your firmware echoes request sequence numbers and you need strict matching. The web dashboard uses command ID matching as well.

See [examples](../examples/README.md), [streaming](streaming.md), and [web UI](WEB_UI.md).
