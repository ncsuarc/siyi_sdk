# A8 Mini web dashboard

Install `pip install -e ".[web]"` and run `python -m web_ui.server`. Open `http://localhost:8082`.

The left side shows the camera state, the main RTSP video stream, and a collapsible SD card media browser. The toolbar above the video turns Live View on or off and selects the Auto, OpenCV, GStreamer, or aiortsp video backend.

The right side has four tabs. The dashboard remembers the last one you used.

- **Control**: gimbal mode, joystick, centering, photo, recording, and digital zoom. On this tab you can also use the keyboard: hold the arrow keys to rotate, Space to stop, C to center, and hold + or - to zoom.
- **Explorer**: the A8 Mini Command Explorer. Search or filter by group, choose a command, enter its typed parameters (numeric ranges come from the SDK signature), and send it. Each result shows server and browser timing and the TX/RX frames logged while it ran. The last 50 runs are listed in History: click a row to reload its arguments and result. You can copy one result or export the whole history as JSON. Read-only `get_` commands can be repeated N times at a fixed interval; the summary reports the failure rate and p50/p95/max reply time, which is useful for spotting unreliable UDP replies.
- **Protocol log**: every SDK frame the dashboard sends or receives, with parse errors. The hex is split into header, payload, and CRC. You can filter by direction or command name/ID, pause, clear, or export. Attitude and function-feedback pushes are hidden by default.
- **Diagnostics**: a 30 s yaw/pitch/roll plot, control latency with a camera reply-time sparkline, and firmware and encoding information.

Formatting the SD card, rebooting, or changing the device IP requires confirmation in a dialog. To format, you must type `FORMAT`. After an IP change, the server reconnects to the new address. The Camera IP field in Settings changes only the address the dashboard connects to; it does not reconfigure the camera.

The browser uses an explicit server-side command registry. Unknown method names and unsupported A8 Mini commands cannot be invoked through the explorer.
