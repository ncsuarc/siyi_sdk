# A8 Mini SDK protocol notes

The [official SIYI protocol](https://siyi.biz/siyi_file/A8%20mini/SIYI_Gimbal_Camera_External_SDK_Protocol_Update_Log%20V0.1.1.pdf) and [A8 Mini manual](https://siyi.biz/siyi_file/A8%20mini/A8%20mini%20User%20Manual%20v1.6.pdf) describe the wire format and which models support each command. A [repository copy](../SIYI_SDK_PROTOCOL.pdf) is also available. The Python package covers:

- System: firmware, hardware ID, UTC time, and soft reboot
- Gimbal: rotation, centering, target angles, mode, attitude, and magnetic encoder
- Camera: photo/video, digital zoom, encoding, SD card, HDMI/CVBS output, and OSD
- Integration: aircraft attitude, raw GPS, and the gimbal data streams

Control uses UDP or TCP on port `37260`, or UART at the configured baud rate. TCP connections need a 1 Hz heartbeat, which the SDK sends. The protocol calls `SEQ` a frame sequence but does not require replies to copy the request's sequence. The A8 Mini camera stamps every reply with its own per-link counter and relays gimbal replies unchanged, so the SDK always matches replies by command ID. The protocol also says the A8 Mini answers single-axis command `0x41` under command ID `0x0E`.

## Commands the A8 Mini does not answer

These were checked against firmware pack v0.4.9 (gimbal v0.4.9, camera v0.3.7) by disassembling the camera's SDK handlers. Commands `0x24`, `0x27`-`0x2A`, `0x31`, `0x40`, `0x49`, `0x4A`, `0x70`, `0x71`, `0x81` and `0x82` have no handler in either dispatch table. Their client methods raise `UnsupportedCommandError` without sending anything. This includes IP get/set: the camera keeps its address in `/customer/network_config.ini` and only reads it at boot. Motor voltage (`0x2A`) cannot be queried, but it is still pushed through the `0x25` data stream.

SD formatting (`0x48`) is acknowledged only once formatting finishes, and never when no card is mounted. `format_sd_card()` therefore waits up to 60 s and never retries.

Absolute zoom (`0x0F`) accepts 1.0x up to a resolution-dependent maximum (720p 6.0x, 1080p 5.5x, 1440p 3.5x, 4K 1.0x). The camera never acknowledges it in 4K. Encoding changes (`0x21`) are acknowledged as successful even when the camera ignores them, so the SDK reads them back with `0x20`.

## Hardware scope

The A8 Mini has no thermal sensor, laser rangefinder, or optical focus motor. Video stitching is for multi-sensor cameras. The RTSP stream is `rtsp://192.168.144.25:8554/main.264`.

The camera status response contains some vendor-wide fields, such as laser and HDR status. The parser keeps those wire fields so packet offsets and decoding stay correct; they do not imply corresponding A8 Mini controls.

AI tracking through the public protocol requires SIYI's AI Tracking Module II. The SDK separately drives the firmware's built-in tracking controller over a private, undocumented channel on port 37256; see [firmware tracking](firmware-tracking.md).
