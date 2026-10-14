# Firmware tracking (experimental)

The A8 Mini firmware contains the tracking controller that SIYI's optional AI Tracking Module II uses. The SDK can drive it directly, without the module, over a private TCP channel on port **37256**. This is separate from the public SDK protocol on port 37260. The SDK sends target boxes, and the gimbal does its own steering and zoom compensation.

This was recovered from camera firmware v0.3.7 / gimbal v0.4.9 (see the [firmware analysis](firmware-ai-tracking-analysis.md)). SIYI does not document it, and other firmware versions may behave differently. Treat it as experimental.

For a lock steered entirely by the SDK through the public protocol, use `GimbalPointLock` from [point lock](tracking.md) instead.

## From a ground station

The camera service on the companion computer handles all of this. With `--video auto` (the default) on supported firmware, it also uses the firmware link as its tracking video, which has lower latency than RTSP. Choose the firmware controller per lock, or make it the default:

```python
await cam.lock(x, y, controller="firmware")
await cam.set_lock_config(control="firmware")
```

On other firmware the lock fails with `ServiceUnavailableError`. `await cam.get_state()` reports `firmware_tracking` under `camera`. See the [ground station guide](remote.md#point-lock). The rest of this page is for using the classes in your own process.

## Pieces

| Class | Module | Role |
| --- | --- | --- |
| `FirmwareTrackingClient` | `siyi_sdk` | Low-level private channel: AI mode on/off, the camera's 1280x720 H.265 video, target boxes |
| `FirmwareLink` | `siyi_sdk.tracking` | Keeps a `FirmwareTrackingClient` connected and decodes its video into frames (needs PyAV: `stream-aiortsp` extra) |
| `FirmwarePointLock` | `siyi_sdk.tracking` | Follows a fixed scene point with optical flow and sends it to the firmware as the target (needs `tracking` extra) |

## Low-level client

```python
from siyi_sdk import FirmwareTrackingClient

tracking = FirmwareTrackingClient("192.168.144.25")
await tracking.connect()              # opens the channel; does not change the mode
try:
    await tracking.set_mode(True)     # AI mode on, confirmed by a read-back
    await tracking.send_target(x=600, y=320, width=80, height=80)   # 1280x720 coordinates
    ...
    await tracking.cancel_target()    # what the AI module sends before switching off
    await tracking.set_mode(False)
finally:
    await tracking.close()
```

Rules the camera imposes:

- It serves **one client** on this port. `close()` resets the connection on purpose, because a graceful close can leave the camera half-open and lock out every later client until it restarts.
- `close()` does not switch AI mode off. Switch it off yourself first, or the gimbal keeps steering to the last target.
- It drops a client that has been silent for 4 s. The client sends a keepalive every second for you.
- Targets are in 1280x720 coordinates whatever the RTSP resolution is. A successful `send_target` means the write went out, not that the gimbal moved.
- There are no retries or automatic reconnection. A failure sets `tracking.error` and `is_connected` becomes `False`.

## Point lock on the firmware controller

`FirmwareLink` supplies the camera's own tracking video, which has lower latency than RTSP. `FirmwarePointLock` turns a pixel into a stream of targets:

```python
from siyi_sdk import connect_udp
from siyi_sdk.tracking import FirmwareLink, FirmwarePointLock

camera = await connect_udp()
latest = {}
lock: FirmwarePointLock | None = None

async def on_frame(image, timestamp):
    latest["image"], latest["time"] = image, timestamp
    if lock is not None and lock.active:
        await lock.update(image, timestamp=timestamp)

async def on_failure(reason: str) -> None:
    if lock is not None:
        await lock.release(reason)            # a reconnect never resumes a lock

link = FirmwareLink("192.168.144.25", on_frame=on_frame, on_failure=on_failure)
link.start()
# wait until link.ready (the video starts at a keyframe, about every 2.5 s)

lock = FirmwarePointLock(
    link.client,
    owns_connection=False,                    # the link keeps the channel open
    stop=lambda: camera.rotate_nowait(0, 0),  # emergency stop over the public SDK
    video_timeout=1.2,                        # this video arrives in bursts
)
image = latest["image"]
await lock.start(image, x=640, y=360, timestamp=latest["time"])   # pixel in that image
...
await lock.release()
await link.stop()                              # also switches AI mode off
await camera.close()
```

Behaviour to plan around:

- Each `FirmwarePointLock` is single-use. Create a new one for every lock.
- The lock releases itself on tracking loss, an off-screen point, stale video, a connection failure, or AI mode being switched off elsewhere. `lock.reason` says why, and `lock.status` has the current state.
- `release()` sends a cancel target, switches AI mode off and confirms it. `exit_confirmed` is `False` if that confirmation failed; then assume the gimbal may still be steering.
- `response` (default 1.0) scales how hard the firmware steers, and `integral` removes its small standing offset. Passing `attitude=AttitudeHistory` with correct `attitude_signs` and `video_delay_s` corrects for delay. A wrong sign drives the gimbal to its limit, so leave it unset until you have measured them.
- `FirmwareLink` delivers grayscale frames by default. Pass `image_format="bgr24"` if you need colour. Its buffer limits and failure behaviour are described under "Performance and image-format compatibility" in [tracking.md](tracking.md).
