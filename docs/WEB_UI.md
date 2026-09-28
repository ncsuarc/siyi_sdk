# A8 Mini web dashboard

Install `pip install -e ".[web]"` and run `python -m web_ui.server`. Open `http://localhost:8082`.

The dashboard shows the main RTSP video stream, gimbal status and controls, photo/record actions, digital zoom, encoding settings, and SD card media. Select Auto, OpenCV, GStreamer, or aiortsp in the Video backend menu.

The **A8 Mini Command Explorer** lists every retained SDK command by group. Choose a command, enter its typed parameters, send it, and inspect the returned value or error. Nested values such as IP settings and GPS data use grouped fields. The live panel shows gimbal attitude and recent function feedback from the camera over the existing WebSocket.

The explorer requires a confirmation before formatting the SD card, rebooting, or changing the device IP. After an IP change, the server reconnects to the new address. The separate Camera settings IP field changes only the address the dashboard connects to; it does not reconfigure the camera.

The browser uses an explicit server-side command registry. Unknown method names and unsupported A8 Mini commands cannot be invoked through the explorer.
