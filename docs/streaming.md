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

With a control client, `camera.create_stream()` builds the same stream for the client's camera IP. Frame callbacks receive a `StreamFrame` with a BGR `frame` array, `width`, `height` and a `time.monotonic()` arrival `timestamp`; `stream.last_frame` holds the newest one. Callbacks may be plain functions or coroutines.

## On a ground station

The camera service doesn't relay video. A ground station using `RemoteCamera` opens the camera's RTSP stream itself with `SIYIStream`, as above, using the camera's address rather than the companion computer's. The camera network must be routed to the ground over the radio link.

To lock on a spot the operator clicks, send the clicked frame's arrival time along with the click. The service then locks on the same picture, even though the ground's video lags behind the aircraft's:

```python
shown = stream.last_frame                         # the frame on screen when the operator clicked
await cam.lock(px / shown.width - 0.5, py / shown.height - 0.5, frame_time=shown.timestamp)
```

See the [ground station guide](remote.md#point-lock).

The debugging dashboard's Video backend selector lets you compare Auto, OpenCV, GStreamer, and aiortsp on your hardware.
