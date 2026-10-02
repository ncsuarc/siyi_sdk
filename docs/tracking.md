# Point lock

Point lock keeps the gimbal pointed at a fixed spot in the scene while the aircraft moves. Install it with `pip install "siyi-sdk[tracking]"` (OpenCV and NumPy). It runs on the CPU; on a 1280-pixel-wide frame it takes about 2 ms per frame on a desktop.

## How it works

- `PointLock` follows the spot by tracking a few hundred features across the whole frame. Features on moving objects are discarded, so passing cars do not drag the spot with them. The spot itself can be featureless, briefly covered, or off-screen.
- `GimbalPointLock` turns the spot's offset from the image centre into an angle and sends rotation-speed commands (`0x07`) through a PI controller. It uses speed rather than angle targets because the A8 Mini reports yaw relative to the aircraft body, and that reading goes stale if the aircraft turns during the video delay.
- Use Lock gimbal mode. The gimbal then cancels aircraft rotation itself, and the loop only has to follow the slower drift caused by the aircraft moving.

```python
from siyi_sdk import connect_udp
from siyi_sdk.tracking import GimbalPointLock, LockGains

client = await connect_udp()
stream = client.create_stream()
lock = GimbalPointLock(client, hfov_deg=81.0, gains=LockGains(kp=2.0))

async def on_frame(frame):
    if lock.active:
        await lock.update(frame.frame, zoom=current_zoom, timestamp=frame.timestamp)

stream.on_frame(on_frame)
await stream.start()
lock.lock(stream.last_frame.frame, x=900, y=400)  # pixel to hold
...
await lock.release()  # stops the gimbal
```

See `examples/point_lock.py` for a complete script and `examples/tracker_demo.py -t point` to try the tracker on a recorded video.

## Choosing the motion model

| `model` | Use for | Why |
| --- | --- | --- |
| `local` (default) | Ground targets seen from above or at an angle, including among buildings | Moves the spot with the 40 features nearest it, which sit at a similar depth |
| `global` | Distant targets near the horizon with foreground in front | The nearest on-screen features are then much closer than the target; the whole-frame motion fits the distant scene better |

## Tuning

- `LockGains.kp` is speed units per degree of error (default 2). The camera does not document degrees per second per unit. Raise `kp` until the camera overshoots the spot, then halve it. In a simulation with 0.2 s of video delay, `kp=2` was stable for gimbals turning 0.5 to 2 degrees per second per unit, and `kp=4` oscillated at 2.
- `hfov_deg` is the horizontal field of view at 1x. Pass the current digital zoom to `update()`, since zooming narrows the view.
- The lock reports `SEARCHING` and holds the gimbal still when it cannot estimate scene motion (for example over sky or water), and releases after `lost_timeout` seconds.

## Limits

- Accuracy is best while the spot stays near the image centre, which the loop maintains. If the spot stays out of view for many seconds, the estimate drifts.
- The video must show a static scene around the spot. Point lock does not follow moving objects; use an object tracker such as `cv2.TrackerNano` for those, and do not rely on its score alone to detect loss.
