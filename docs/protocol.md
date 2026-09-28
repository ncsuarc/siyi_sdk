# A8 Mini SDK protocol notes

The [bundled SIYI protocol reference](../SIYI_SDK_PROTOCOL.pdf) is vendor-wide. The Python package implements the commands relevant to the A8 Mini:

- System: firmware, hardware ID, system time, network configuration, and soft reboot
- Gimbal: rotation, centering, target angles, mode, attitude, and magnetic encoder
- Camera: photo/video, digital zoom, encoding, SD card, picture naming, HDMI/CVBS output, and OSD
- Integration: aircraft attitude, raw GPS, flight-controller and gimbal streams, and ArduPilot diagnostics

Control uses UDP or TCP on port `37260`, or UART at the configured baud rate. TCP connections use a 1 Hz heartbeat. The wire frame parser, sequence matching, CRC, and transport recovery remain shared across the retained commands.

The A8 Mini has no thermal sensor, laser rangefinder, or optical focus motor. AI tracking requires the optional SIYI AI Module II and is outside this SDK. Video stitching is for multi-sensor cameras. The A8 Mini RTSP stream is `rtsp://192.168.144.25:8554/main.264`.

The camera status response contains some vendor-wide fields, such as laser and HDR status. The parser retains those wire fields so packet offsets and decoding stay correct; they do not imply corresponding A8 Mini controls.
