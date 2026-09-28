# A8 Mini RTSP streaming

The A8 Mini has one RTSP URL: `rtsp://192.168.144.25:8554/main.264`. Use `build_rtsp_url(host)` to build it for another camera IP. There is no second RTSP URL exposed by this SDK. `StreamType.SUB` refers to camera encoding configuration, not to an RTSP sub stream.

## Backends

| Backend | Install extra | Notes |
| --- | --- | --- |
| OpenCV | `stream-opencv` | Easiest setup |
| GStreamer | `stream-gst` | Requires system GStreamer and PyGObject libraries; see `install_gst_dependencies.sh` |
| aiortsp + PyAV | `stream-aiortsp` | Python RTSP session and PyAV decoder |

`StreamBackend.AUTO` tries GStreamer, then aiortsp, then OpenCV. Startup succeeds only after a decoded frame arrives. The stream reports state and last error after startup.

```python
from siyi_sdk import SIYIStream, StreamBackend, StreamConfig, build_rtsp_url

stream = SIYIStream(StreamConfig(build_rtsp_url(), backend=StreamBackend.AUTO))
stream.on_frame(lambda frame: print(frame.width, frame.height))
await stream.start()
# Read frames until done.
await stream.stop()
```

The web UI uses the same URL. Its Video backend selector lets you compare Auto, OpenCV, GStreamer, and aiortsp without changing the SDK code.
