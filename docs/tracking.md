# Point lock

Point lock keeps the gimbal pointed at a fixed spot in the scene while the aircraft moves. Install it with `pip install "siyi-sdk[tracking]"` (OpenCV and NumPy). It runs on the CPU; on a 1280-pixel-wide frame the tracker takes about 2 ms per frame on a desktop.

## How it works

- `PointLock` follows the spot by tracking a few hundred features across the whole frame. Features on moving objects are discarded, so passing cars do not drag the spot with them. The spot itself can be featureless, briefly covered, or off-screen.
- `GimbalPointLock` steers the gimbal to keep the spot centred. Use Lock gimbal mode, so the gimbal itself cancels aircraft rotation.

The hard part is delay. A decoded frame shows the scene as it was 150-300 ms ago (encoding, RTSP, decoding), and a controller that steers on that old picture has to be slow or it oscillates. The attitude stream (`0x25`, up to 100 Hz) arrives almost immediately, so `GimbalPointLock` combines the two:

1. Each frame gives the spot's offset from the image centre at the moment the frame was captured (arrival time minus `LoopModel.video_delay_s`).
2. Adding the gimbal attitude at that same moment, from `AttitudeHistory`, gives the spot's direction in gimbal angles. That direction doesn't depend on where the gimbal has turned since.
3. A straight-line fit over the last 0.4 s of these directions gives the spot's angular velocity (a ground spot drifts across the sky as the aircraft flies past).
4. A 50 Hz control loop predicts the spot's direction now, compares it with the newest attitude sample, and acts:
   - `control="angle"`: sends the predicted direction as a `0x0E` angle target and lets the gimbal's own position controller do the fast work. There is nothing to tune.
   - `control="rate"`: PI on the predicted error plus velocity feedforward, sent as `0x07` speeds. Gains follow from the measured delay (`LockGains.for_model`).

Without an `AttitudeHistory`, the lock falls back to a slower PI loop on the raw, delayed image error, once per frame.

```python
from siyi_sdk import connect_udp
from siyi_sdk.models import DataStreamFreq, GimbalDataType
from siyi_sdk.tracking import AttitudeHistory, GimbalPointLock, LoopModel

client = await connect_udp()
history = AttitudeHistory()
history.attach(client)
await client.request_gimbal_stream(GimbalDataType.ATTITUDE, DataStreamFreq.HZ50)

loop = LoopModel(deg_per_unit=(1.0, 1.0), command_delay_s=0.1, video_delay_s=0.2)
lock = GimbalPointLock(client, attitude=history, loop=loop, control="angle")

stream = client.create_stream()

async def on_frame(frame):
    if lock.active:
        await lock.update(frame.frame, zoom=current_zoom, timestamp=frame.timestamp)

stream.on_frame(on_frame)
await stream.start()
lock.lock(stream.last_frame.frame, x=900, y=400)  # pixel to hold
...
await lock.release()
```

`examples/point_lock.py` is a complete script; `examples/tracker_demo.py -t point` tries the tracker on a recorded video.

## Measuring the loop

`calibrate_loop` turns the gimbal about 20 degrees on each axis and back while recording the attitude stream and how far the picture shifts between frames (phase correlation). It returns:

| Measured | How |
| --- | --- |
| Turn rate per `0x07` unit, per axis | Slope of the attitude while turning |
| Command delay | Where that slope meets the starting attitude (dead time plus motor lag) |
| Video delay | Time shift that best lines up picture motion with attitude |
| Attitude direction convention | Sign of that fit |
| Field of view | Scale of that fit |

Point the camera at a textured, still scene. The dashboard runs this from **Settings → Point and drag → Measure loop timing** and saves the results.

## Robustness

The video delay matters most. If it is set too long, the attitude paired with each frame is from too early; while the gimbal turns, that makes the fixed spot appear to move, and the velocity prediction feeds that back. In simulation, a delay 60 ms too long made both modes run away. So:

- While locked, the lock re-estimates the video delay every few frames: it tries delays around the current one and keeps the one under which the spot's direction is most nearly a straight line in time. This only happens when the gimbal has turned enough to tell delays apart.
- Until that estimate has confirmed the delay, a 20% shorter delay is used, because too short only slows the loop. Pass `trust_video_delay=True` when the delay was measured.
- The spot's estimated velocity is limited to 30 degrees per second.
- With speed commands (`control="rate"`), the lock also learns the turn rate and the command dead time (`TurnRateEstimator`): over 0.2 s windows it compares how far the attitude stream shows the gimbal turning with how far the calibrated model predicts, and corrects `deg_per_unit` by the ratio (0.5x to 2x). Windows with too little commanded motion, full-speed commands, or turns far from the prediction (aircraft yaw, a hand on the rig) are ignored. The dead time is refined like the video delay, within 0.5x to 1.5x of the measured one. Pass the same `turn_rate` estimator to the next `GimbalPointLock` to continue learning; `lock.adapt_turn_rate = False` turns it off.
- If the error keeps swinging past +-0.5 degrees (four swings within 3 s), the controller's `OscillationGuard` cuts the gain to 70% (down to 30% at most) and restores it slowly after 4 s of calm. `LockStatus.turn_scale` and `gain_scale` show both adaptations.

A 25% error in the turn rate costs little: angle mode doesn't use it, and the rate loop's integral absorbs it.

## What the tests cover

`tests/tracking/sim.py` simulates a gimbal (command delay, motor lag, speed and angle modes, 50 Hz attitude) and a camera (30 fps, delivered late) over a textured scene while the aircraft drifts. `tests/tracking/test_closed_loop.py` checks, in real time against that simulation, that:

- both modes settle within 1 degree in 1.2 s from an 11 degree offset and track a spot drifting at 8.5 degrees per second;
- the lock recovers from a video delay set 60 ms too short or too long;
- calibration recovers the simulated turn rate, delays, axis directions and field of view.

The simulation is a model, not the A8 Mini. How the real gimbal responds to a 50 Hz stream of `0x0E` targets, its real delays, attitude noise and packet loss are not covered; measure on hardware and compare the two control modes there.

## Choosing the motion model

| `model` | Use for | Why |
| --- | --- | --- |
| `local` (default) | Ground targets seen from above or at an angle, including among buildings | Moves the spot with the 40 features nearest it, which sit at a similar depth |
| `global` | Distant targets near the horizon with foreground in front | The nearest on-screen features are then much closer than the target; the whole-frame motion fits the distant scene better |

## Limits

- Accuracy is best while the spot stays near the image centre, which the loop maintains. If the spot stays out of view for many seconds, the estimate drifts.
- The A8 Mini reports yaw relative to the aircraft body. A steady aircraft turn looks like the spot moving and is followed; a sudden yaw shows up as a short pointing error.
- Point lock does not follow moving objects; use an object tracker such as `cv2.TrackerNano` for those, and do not rely on its score alone to detect loss.

## Performance and image-format compatibility

`FirmwareLink` now delivers full-resolution, single-channel `uint8` grayscale images by default. `PointLock`, `GimbalPointLock`, `FirmwarePointLock`, and calibration recording accept both grayscale and BGR images. Image coordinates and ROI sizes still refer to the original resolution. Consumers requiring color or an `(H, W, 3)` array must pass `image_format="bgr24"`; the ground dashboard does this explicitly. RTSP `StreamFrame` remains BGR.

The firmware link decodes compressed packets in order, but a slow frame callback receives only the newest pending decoded picture. Each picture carries the timestamp associated with its decoded packet, including when the decoder delays output. An overload invalidates pending pictures and fails the session. Supply an asynchronous `on_failure(reason)` callback that releases your tracking owner; automatic reconnection switches AI mode off and must not restart a lock.

Provisional limits are configurable constructor keywords:

| Component | Option | Default |
| --- | --- | --- |
| `ThreadedTCPTransport` | `max_buffer_bytes`, `max_buffer_chunks` | 4 MiB, 256 chunks |
| `FirmwareLink` | `max_packet_bytes`, `max_packets` | 8 MiB, 256 access units, including active decode batches |
| `FirmwareLink` | `max_queue_age` | 0.4 seconds from local receipt |
| `FirmwareLink` | `batch_packets`, `batch_bytes` | 8 access units, 1 MiB; one indivisible access unit may exceed a custom smaller batch-byte limit |
| `FirmwareLink` | `decoder_threads` | 0 (codec-selected count), with slice threading |

Raw TCP limits apply backpressure and preserve all bytes. Compressed limits fail the session instead of dropping dependent encoded frames. These limits exclude OS socket buffers, the receive staging buffer, native decoder allocations, and decoded pictures. The decoded handoff retains one pending picture plus the picture in use by the callback; active decode/conversion can temporarily retain another picture. Local receipt age is not camera exposure age.

Stopping waits up to one second for in-flight native decoding. If the job remains alive, the link stays failed and refuses `start()` until that job finishes; cancellation does not forcibly terminate native code. A caller must not repeatedly replace a failed link with new instances to bypass this ownership guard.

For asynchronous calibration callbacks, use `await recorder.add_async(image, timestamp)`. It serializes native work and drains it on cancellation. Use `await recorder.stop_async()` before accessing the completed recording or calling `start()` again. The existing synchronous `add`/`stop` methods remain available; do not mix synchronous writes with asynchronous recording jobs.

`await lock.metrics.summary_async()` moves final summary work off the callback; call it after stopping the lock. Exact whole-lock p95 and event medians are preserved. Numeric storage is more compact, but exact lifetime statistics and temporary sorting memory still grow with lock duration. Python worker threads can still contend for the GIL.

See [performance results](performance-results.md) for reproducible local measurements, rejected candidates, and remaining Pi validation.
