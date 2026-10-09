# A8 Mini web dashboard

With [uv](https://docs.astral.sh/uv/) (recommended, no manual venv or Python install needed):

```
uv sync --extra web --extra tracking
uv run python -m web_ui.server
```

Or with pip: `pip install -e ".[web]"` and `python -m web_ui.server`. Open `http://localhost:8082`.

Supported Python versions are 3.10 to 3.13.

The left side shows the camera state, the main RTSP video stream, and a collapsible SD card media browser. The toolbar above the video turns Live View on or off and selects the Auto, OpenCV, GStreamer, or aiortsp video backend.

The right side has four tabs. The dashboard remembers the last one you used.

- **Control**: gimbal mode, centering, photo, recording, and digital zoom. On this tab you can also use the keyboard: hold the arrow keys to rotate, Space to stop, C to center, and hold + or - to zoom. Over the video, a trackpad two-finger scroll turns the gimbal and pinch zooms.

  With **Point and drag** on (toolbar above the video), click the video to turn the camera toward that spot, drag to pan the scene under the cursor, and scroll to zoom toward the cursor. These send absolute angle targets (`0x0E`) computed from the field of view, the current zoom, and the attitude from when the frame was shown. Calibrate the field of view, video delay, and axis directions under **Settings → Point and drag**; the browser remembers them and sends them to the server on load.

  To keep the camera on a fixed spot while the drone moves, set **Click video to** to **Lock on spot** and click the spot. The server tracks the spot in the live video and steers the gimbal to keep it centred; a reticle on the video shows the spot, or an arrow when it is off-screen. Scrolling zooms without losing the lock. Release it with **Release lock**, Esc, Stop, the joystick, arrow keys, centering, or a pointer aim or drag. Before relying on it, run **Settings → Point and drag → Measure loop timing** with the camera on a textured, still scene: the gimbal turns about 20° on each axis and back, and the dashboard stores the measured turn rate, delays, axis directions, and field of view. Steering uses angle targets by default; speed commands are the alternative. With speed commands the lock keeps learning the real turn rate and command delay (they drift with battery and payload) and backs off its gain if it starts to ring; the lock badge shows both once they differ, and **Save what locks learned** stores them as the measured values. See [tracking.md](tracking.md). Use Lock gimbal mode.
- **Explorer**: the A8 Mini Command Explorer. Search or filter by group, choose a command, enter its typed parameters (numeric ranges come from the SDK signature), and send it. Each result shows server and browser timing and the TX/RX frames logged while it ran. The last 50 runs are listed in History: click a row to reload its arguments and result. You can copy one result or export the whole history as JSON. Read-only `get_` commands can be repeated N times at a fixed interval; the summary reports the failure rate and p50/p95/max reply time, which is useful for spotting unreliable UDP replies.
- **Protocol log**: every SDK frame the dashboard sends or receives, with parse errors. The hex is split into header, payload, and CRC. You can filter by direction or command name/ID, pause, clear, or export. Attitude and function-feedback pushes are hidden by default.
- **Diagnostics**: a 30 s yaw/pitch/roll plot, control latency with a camera reply-time sparkline, and firmware and encoding information.

Formatting the SD card, rebooting, or changing the device IP requires confirmation in a dialog. To format, you must type `FORMAT`. After an IP change, the server reconnects to the new address. The Camera IP field in Settings changes only the address the dashboard connects to; it does not reconfigure the camera.

The browser uses an explicit server-side command registry. Unknown method names and unsupported A8 Mini commands cannot be invoked through the explorer.
